#!/usr/bin/env python3
"""What a repository's code declares: API routes, data tables, GraphQL and RPC operations, jobs.

    python code_surface.py --repo REPO

A read-only pass beside discover_repo (which reads manifests). It recognizes common
declarations by their syntax, without running or importing anything, and names the
file and line of each:

- routes: Next.js app and pages routers (pages as PAGE), Remix / React Router file
  routes, Express/Fastify/Koa/Hono, Flask and FastAPI (with router prefixes), Django
  urls.py, Go (net/http, chi, gin, echo, gorilla), Spring (with class-level prefixes),
  Rails (with namespaces and nesting), Laravel (with route groups);
- operations: GraphQL Query/Mutation/Subscription fields, tRPC procedures;
- tables: Prisma models, SQL CREATE TABLE, Django, SQLAlchemy and SQLModel models,
  Drizzle, TypeORM, Mongoose, Sequelize, Rails schema.rb, Laravel migrations, JPA
  @Entity/@Table;
- jobs: Celery tasks and beat schedules, Rails ActiveJob and Sidekiq jobs, Laravel
  queued jobs and schedules, Spring @Scheduled, sidekiq-cron style schedule.yml
  entries, Vercel crons, BullMQ queues, node-cron, Inngest and trigger.dev tasks.

With the Rust engine (`ontologyex-discover surface`), Python, JavaScript/TypeScript and Go
files are read from syntax trees instead of patterns: comments and strings are not code,
calls and decorators may span lines, and each router's own prefix is followed (FastAPI and
Flask routers, Express `app.use`, gin/echo groups, chi `Route`, gorilla `PathPrefix` and
`.Methods`), as are chained and Fastify-style routes and re-exported Next.js handlers. The
other languages, and every file when the engine is missing, are read by the patterns below.

Each finding is a candidate from syntax, not a verified endpoint or table: a route
may be disabled, a table dropped by a later migration, and code built at runtime is
not seen. `attach` adds routes and tables to a discovered topology as map nodes,
each under the app whose folder holds it.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
from urllib.parse import quote

SKIP_DIRS = {".git", ".hg", "node_modules", ".venv", "venv", "env", "__pycache__", "dist", "build", "target",
             "vendor", ".next", ".nuxt", ".tox", ".mypy_cache", "site-packages", "bower_components", ".cache",
             "coverage", "__snapshots__", "testdata", "fixtures", ".turbo", ".svelte-kit", "out"}
CODE = {".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".go", ".java", ".kt", ".rb", ".php",
        ".prisma", ".sql", ".graphql", ".gql"}
SCHEDULE_FILE = re.compile(r"^(?:schedule|sidekiq_cron|sidekiq-cron|cron|crontab)\.ya?ml$|^vercel\.json$")
MAX_FILES = 50_000
MAX_FILE = 1024 * 1024
MAX_SCHEMA_FILE = 4 * 1024 * 1024        # .graphql, .prisma and .sql schemas can be large
MAX_TOTAL = 96 * 1024 * 1024
MAX_PER_KIND = 500
MAX_MAP_PER_KIND = 256                   # map nodes per kind; the rest are counted, not drawn
METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")

NEXT_APP_METHOD = re.compile(r"^export\s+(?:async\s+)?(?:function|const)\s+(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\b", re.M)
JS_ROUTE = re.compile(r"\b(?:app|router|server|api|routes?|fastify|hono|r|v\d|\w+Router)\s*\.\s*"
                      r"(get|post|put|patch|delete|all)\s*\(\s*(['\"`])(/[^'\"`\s]*)\2")
PY_ROUTE = re.compile(r"^[ \t]*@\s*[\w.]+\.(route|get|post|put|patch|delete|api_route)\s*\(\s*(?:path\s*=\s*)?"
                      r"[rfub]?['\"](/[^'\"]*)['\"]([^\n]*)", re.M)
PY_METHODS = re.compile(r"methods\s*=\s*[\[(]([^\])]*)[\])]")
DJANGO_PATH = re.compile(r"\b(?:re_)?path\s*\(\s*r?['\"]([^'\"]*)['\"]")
GO_ROUTE = re.compile(r"\.\s*(GET|POST|PUT|PATCH|DELETE|Get|Post|Put|Patch|Delete|Handle|HandleFunc|Any|Method)\s*\(\s*"
                      r"(?:\"(GET|POST|PUT|PATCH|DELETE)\"\s*,\s*)?\"((?:(?:GET|POST|PUT|PATCH|DELETE)\s+)?/[^\"]*)\"")
SPRING_ROUTE = re.compile(r"@(Get|Post|Put|Delete|Patch|Request)Mapping\b(?:\s*\(([^)]*)\))?")
SPRING_SCHEDULED = re.compile(r"@Scheduled\b[^\n]*\n(?:\s*@[^\n]*\n)*\s*(?:(?:public|protected|private|open|override)\s+)*"
                              r"(?:void\s+|fun\s+)(\w+)\s*\(")
JPA_ENTITY = re.compile(r"@Entity\b(?:\s*\([^)]*\))?")
LARAVEL_ROUTE = re.compile(r"(?:Route::|\$router->|->)(get|post|put|patch|delete|any|options|resource|apiResource)"
                           r"\s*\(\s*['\"]([^'\"]*)['\"]")
LARAVEL_TABLE = re.compile(r"Schema::create\s*\(\s*['\"](\w+)['\"]")
LARAVEL_JOB = re.compile(r"^[ \t]*(?:final\s+)?class\s+(\w+)[^{]*\bimplements\b[^{]*\bShouldQueue\b", re.M)
LARAVEL_SCHEDULE = re.compile(r"(?:Schedule::|\$schedule->)(command|job|call|exec)\s*\(\s*(?:new\s+)?['\"]?([\w:\\ -]+?)"
                              r"(?:::class|['\"])")
GRAPHQL_BLOCK = re.compile(r"^(?:extend\s+)?type\s+(Query|Mutation|Subscription)\b[^{]*\{", re.M)
GRAPHQL_FIELD = re.compile(r"^\s{1,8}(\w+)\s*[(:]")
TRPC = re.compile(r"(?:\bconst\s+(\w+)\s*=|^[ \t]*(\w+)\s*:)\s*(\w*[pP]rocedure)\b", re.M)
PRISMA_MODEL = re.compile(r"^model\s+(\w+)\s*\{", re.M)
SQL_TABLE = re.compile(r"\bCREATE\s+(?:UNLOGGED\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:[\"`]?\w+[\"`]?\.)?[\"`]?(\w+)[\"`]?", re.I)
DRIZZLE = re.compile(r"\b(?:pg|mysql|sqlite)Table\s*\(\s*['\"](\w+)['\"]")
TYPEORM = re.compile(r"@Entity\s*\(\s*(?:['\"](\w+)['\"])?[^)]*\)\s*(?:export\s+)?(?:default\s+)?class\s+(\w+)")
MONGOOSE = re.compile(r"\bmodel\s*(?:<[^>]*>)?\s*\(\s*['\"](\w+)['\"]\s*,")
SEQUELIZE = re.compile(r"\.define\s*\(\s*['\"](\w+)['\"]")
DJANGO_MODEL = re.compile(r"^class\s+(\w+)\s*\(([^)]*Model[^)]*)\)\s*:", re.M)
NOT_DJANGO_BASES = re.compile(r"\b(?:BaseModel|SQLModel|RootModel)\b")  # pydantic and SQLModel schemas
SQLMODEL_TABLE = re.compile(r"^class\s+(\w+)\s*\([^)]*\btable\s*=\s*True[^)]*\)\s*:", re.M)
ROUTER_PREFIX = re.compile(r"\b(?:APIRouter|Blueprint)\s*\([^)]*?\b(?:url_)?prefix\s*=\s*['\"]([^'\"]*)['\"]", re.S)
SQLALCHEMY = re.compile(r"__tablename__\s*=\s*['\"](\w+)['\"]")
RAILS_TABLE = re.compile(r"^[ \t]*create_table\s+['\"](\w+)['\"]", re.M)
JPA_TABLE = re.compile(r"@Table\s*\(\s*name\s*=\s*\"(\w+)\"")
CELERY = re.compile(r"^[ \t]*@\s*(?:[\w.]+\.)?(?:shared_task|task)\b[^\n]*\n(?:\s*@[^\n]*\n)*\s*(?:async\s+)?def\s+(\w+)", re.M)
BULLMQ = re.compile(r"\bnew\s+(?:Queue|Worker)\s*\(\s*['\"]([\w:.-]+)['\"]")
RUBY_JOB = re.compile(r"^[ \t]*class\s+([\w:]+)\s*<\s*(?:ApplicationJob|ActiveJob::Base)\b|"
                      r"^[ \t]*class\s+([\w:]+)[^\n]*\n(?:[^\n]*\n){0,3}?[ \t]*include\s+Sidekiq::(?:Job|Worker)\b", re.M)
YAML_CRON = re.compile(r"^([\w.-]+):[ \t]*(?:#[^\n]*)?\n((?:[ \t]+[^\n]*\n?|[ \t]*\n)+)", re.M)
CELERY_BEAT = re.compile(r"['\"]([\w.:-]+)['\"]\s*:\s*\{\s*['\"]task['\"]\s*:\s*['\"]([\w.]+)['\"]")
NODE_CRON = re.compile(r"\bcron\.schedule\s*\(\s*['\"]([^'\"]+)['\"]")
TRIGGER_DEV = re.compile(r"\b(?:schedules\.)?task\s*\(\s*\{\s*id\s*:\s*['\"]([\w:.-]+)['\"]")
INNGEST = re.compile(r"\.createFunction\s*\(\s*\{\s*id\s*:\s*['\"]([\w:.-]+)['\"]")
REMIX_LOADER = re.compile(r"^export\s+(?:async\s+)?(?:function|const)\s+loader\b", re.M)
REMIX_ACTION = re.compile(r"^export\s+(?:async\s+)?(?:function|const)\s+action\b", re.M)
TREE_SUFFIXES = (".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".go")  # what the engine's grammars read


def _line(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def walk(repo: Path) -> list[str]:
    paths, base = [], len(str(repo).rstrip(os.sep)) + 1
    for root, dirs, files in os.walk(repo):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        prefix = root[base:].replace(os.sep, "/")
        for name in sorted(files):
            if os.path.splitext(name)[1].lower() in CODE or SCHEDULE_FILE.match(name):
                paths.append(f"{prefix}/{name}" if prefix else name)
                if len(paths) > MAX_FILES:
                    raise ValueError(f"more than {MAX_FILES} source files; point --repo at a smaller folder")
    return paths


def _next_route(rel: str) -> str | None:
    """The URL path a Next.js app-router route file serves, or None."""
    parts = PurePosixPath(rel).parts
    if "app" not in parts[:-1]:
        return None
    segments = parts[len(parts) - parts[::-1].index("app"):-1]  # after the last app/ folder
    kept = [s for s in segments if not (s.startswith("(") and s.endswith(")")) and not s.startswith("@")]
    return "/" + "/".join(kept)


def _rails_routes(text: str):
    """(method, path, offset) for config/routes.rb, with namespaces, scopes and nesting followed."""
    stack: list[tuple[str, str, str]] = []  # paths for nested, member and collection routes
    offset = 0
    for raw in text.splitlines(keepends=True):
        at, offset = offset, offset + len(raw)
        line = raw.split("#", 1)[0].strip()
        opens = re.search(r"\bdo\s*(\|[^|]*\|)?$", line) is not None
        parts: list[str] = []
        owners = []
        for entry in stack:
            if entry[0] in ("@member", "@collection") and owners:  # /chats/:id/retry, /chats/search
                parts[-1] = owners[-1][1 if entry[0] == "@member" else 2]
            elif entry[0] not in ("@member", "@collection"):
                parts.append(entry[0])
                owners.append(entry)
        prefix = "".join(parts)
        pushed = ("", "", "")
        if line == "end":
            if stack:
                stack.pop()
            continue
        if m := re.match(r"namespace\s+:(\w+)", line):
            pushed = ("/" + m.group(1),) * 3
        elif m := re.match(r"scope\b(?:\s*\(?\s*['\"](/?[^'\"]*)['\"]|.*\bpath:\s*['\"](/?[^'\"]*)['\"])?", line):
            pushed = ("/" + (m.group(1) or m.group(2)).strip("/") if (m.group(1) or m.group(2)) else "",) * 3
        elif m := re.match(r"(resources?)\s+:(\w+)", line):
            name = m.group(2)
            yield "ANY", f"{prefix}/{name}", at
            if m.group(1) == "resources":
                singular = re.sub(r"ies$", "y", name) if name.endswith("ies") else re.sub(
                    r"(ss|x|ch|sh)es$", r"\1", name) if re.search(r"(ss|x|ch|sh)es$", name) else re.sub(r"s$", "", name)
                pushed = (f"/{name}/:{singular}_id", f"/{name}/:id", f"/{name}")
            else:
                pushed = (f"/{name}",) * 3
        elif line.startswith(("member", "collection")):
            pushed = ("@" + line.split()[0], "", "")
        elif m := re.match(r"(get|post|put|patch|delete|match)\s*\(?\s*[:'\"]/?([^'\",\s)]*)", line):
            on = re.search(r"\bon:\s*:(member|collection)", line)
            base = ("".join(parts[:-1]) + owners[-1][1 if on.group(1) == "member" else 2]) if on and owners else prefix
            yield ("ANY" if m.group(1) == "match" else m.group(1).upper()), f"{base}/{m.group(2)}".replace("//", "/"), at
        elif line.startswith("root"):
            yield "GET", prefix or "/", at
        elif m := re.match(r"mount\s+[\w:.]+(?:\s*=>\s*|.*\bat:\s*)['\"](/[^'\"]*)['\"]", line):
            yield "ANY", m.group(1), at
        if opens:
            stack.append(pushed)


def _laravel_routes(rel: str, text: str):
    """(method, path, offset) for Laravel routes, with prefix(...)->group(...) blocks followed.

    routes/api.php is served under /api by Laravel's default route setup.
    """
    if "Route::" not in text and "$router->" not in text:
        return
    base = "/api" if PurePosixPath(rel).name == "api.php" and "routes" in PurePosixPath(rel).parts else ""
    stack: list[tuple[int, str]] = []  # (brace depth when the group opened, its prefix)
    depth, statement, start = 0, "", 0
    for match in re.finditer(r"[^\n]*\n?", text):
        line = match.group(0)
        if not statement:
            start = match.start()
        statement += line
        code = line.split("//", 1)[0]
        if re.search(r"->group\s*\(|Route::group\s*\(", statement) and "{" in code:
            prefix = re.search(r"prefix\s*\(\s*['\"]([^'\"]*)['\"]|['\"]prefix['\"]\s*=>\s*['\"]([^'\"]*)['\"]", statement)
            stack.append((depth, "/" + (prefix.group(1) or prefix.group(2)).strip("/") if prefix else ""))
            statement = ""
        prefix = base + "".join(p for _, p in stack if p != "/")
        complete = not statement.strip().endswith(("(", ",")) and re.search(r"Route::|\$router->", statement)
        for route in LARAVEL_ROUTE.finditer(statement if complete else ""):
            verb = "ANY" if route.group(1) in ("any", "resource", "apiResource") else route.group(1).upper()
            path = route.group(2).strip("/")
            yield verb, (f"{prefix}/{path}" if path else prefix or "/").replace("//", "/"), start + route.start()
        if not statement.strip().endswith(("(", ",", "[")):
            statement = ""
        depth += code.count("{") - code.count("}")
        while stack and depth <= stack[-1][0]:
            stack.pop()


def _remix_route(rel: str) -> str | None:
    """The URL path of a Remix / React Router file route under app/routes/, or None."""
    parts = PurePosixPath(rel).parts
    at = next((i for i in range(len(parts) - 2) if parts[i] == "app" and parts[i + 1] == "routes"), None)
    if at is None:
        return None
    rest = list(parts[at + 2:])
    stem = PurePosixPath(rest[-1]).stem
    pieces = [folder[:-1] if folder.endswith("+") else folder for folder in rest[:-1]]
    if stem != "route" or not pieces:  # app/routes/foo/route.tsx serves foo
        pieces.append(stem)
    segments = []
    for piece in pieces:
        for segment in piece.split("."):
            if segment in ("_index", "index", "") or segment.startswith("_"):
                continue  # index routes and pathless layouts add no URL segment
            segment = segment.rstrip("_")
            if segment.startswith("(") and segment.endswith(")"):
                segment = segment[1:-1] + "?"
            segments.append("*" if segment == "$" else ":" + segment[1:] if segment.startswith("$") else segment)
    return "/" + "/".join(segments)


def _pages_page(rel: str) -> str | None:
    """The URL path a Next.js pages-router page serves (not /api, not _app/_document), or None."""
    parts = PurePosixPath(rel).with_suffix("").parts
    if "pages" not in parts[:-1] or "components" in parts:
        return None
    rest = parts[parts.index("pages") + 1:]
    if rest[0] == "api" or any(x.startswith("_") for x in rest):
        return None
    path = "/" + "/".join(rest)
    return (path[:-len("/index")] if path.endswith("/index") else path) or "/"


def _pages_api(rel: str) -> str | None:
    parts = PurePosixPath(rel).with_suffix("").parts
    if "pages" not in parts:
        return None
    rest = parts[parts.index("pages") + 1:]
    if not rest or rest[0] != "api":
        return None
    path = "/" + "/".join(rest)
    return path[:-len("/index")] if path.endswith("/index") else path


def scan_file(rel: str, text: str, facts: dict | None = None) -> dict[str, list[dict]]:
    """One file's declarations. `facts`: what the engine read from its syntax tree (replaces the patterns)."""
    found: dict[str, list[dict]] = {"routes": [], "operations": [], "tables": [], "jobs": []}
    suffix = Path(rel).suffix.lower()
    name = Path(rel).name

    def route(method: str, path: str, offset: int, framework: str, line: int | None = None) -> None:
        found["routes"].append({"method": method.upper(), "path": path, "file": rel,
                                "line": line if line is not None else _line(text, offset), "framework": framework})

    if facts is not None:
        exports: dict[str, int] = {}
        for row in facts.get("exports", []):
            exports.setdefault(row["name"], row["line"])
        if suffix in (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"):
            stem = Path(rel).stem
            if (path := _remix_route(rel)) is not None:
                verbs = [(v, exports[n]) for v, n in (("GET", "loader"), ("POST", "action")) if n in exports]
                for verb, line in verbs or [("PAGE", 1)]:
                    route(verb, path, 0, "remix", line)
            elif stem == "page" and suffix in (".tsx", ".jsx", ".js", ".ts") and (path := _next_route(rel)) is not None:
                route("PAGE", path, 0, "next")
            elif stem == "route" and (path := _next_route(rel)) is not None:
                methods = [r for r in facts.get("exports", []) if r["name"] in METHODS]
                for row in methods:
                    route(row["name"], path, 0, "next", row["line"])
                if not methods:
                    route("ANY", path, 0, "next")
            elif (path := _pages_api(rel)) is not None:
                route("ANY", path, max(0, text.find("export default")), "next")
            elif suffix in (".tsx", ".jsx") and (path := _pages_page(rel)) is not None:
                route("PAGE", path, max(0, text.find("export default")), "next")
        for row in facts.get("routes", []):
            route(row["method"], row["path"], 0, row["framework"], row["line"])
        for kind in ("operations", "tables", "jobs"):
            found[kind].extend({**row, "file": rel} for row in facts.get(kind, []))
        return found

    def table(table_name: str, offset: int, source: str) -> None:
        found["tables"].append({"name": table_name, "file": rel, "line": _line(text, offset), "source": source})

    if suffix in (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"):
        stem = Path(rel).stem
        if (path := _remix_route(rel)) is not None:
            verbs = [v for v, pattern in (("GET", REMIX_LOADER), ("POST", REMIX_ACTION)) if pattern.search(text)]
            for verb in verbs or ["PAGE"]:
                match = (REMIX_LOADER if verb == "GET" else REMIX_ACTION).search(text) if verb != "PAGE" else None
                route(verb, path, match.start() if match else 0, "remix")
        elif stem == "page" and suffix in (".tsx", ".jsx", ".js", ".ts") and (path := _next_route(rel)) is not None:
            route("PAGE", path, 0, "next")
        elif stem == "route" and (path := _next_route(rel)) is not None:
            methods = list(NEXT_APP_METHOD.finditer(text))
            for match in methods:
                route(match.group(1), path, match.start(), "next")
            if not methods:
                route("ANY", path, 0, "next")
        elif (path := _pages_api(rel)) is not None:
            route("ANY", path, max(0, text.find("export default")), "next")
        elif suffix in (".tsx", ".jsx") and (path := _pages_page(rel)) is not None:
            route("PAGE", path, max(0, text.find("export default")), "next")
        for match in JS_ROUTE.finditer(text):
            route(match.group(1), match.group(3), match.start(), "node")
        for match in TRPC.finditer(text):
            if (match.group(1) or match.group(2)).lower().endswith("procedure"):
                continue  # a procedure builder (authedProcedure = t.procedure.use(...)), not an operation
            found["operations"].append({"kind": "rpc", "name": match.group(1) or match.group(2), "file": rel,
                                        "line": _line(text, match.start())})
        for pattern, source in ((DRIZZLE, "drizzle"), (MONGOOSE, "mongoose"), (SEQUELIZE, "sequelize")):
            for match in pattern.finditer(text):
                table(match.group(1), match.start(), source)
        for match in TYPEORM.finditer(text):
            table(match.group(1) or match.group(2), match.start(), "typeorm")
        for pattern, kind in ((BULLMQ, "bullmq queue"), (NODE_CRON, "cron schedule"), (INNGEST, "inngest function"),
                              (TRIGGER_DEV, "trigger.dev task")):
            for match in pattern.finditer(text):
                found["jobs"].append({"name": match.group(1), "file": rel, "line": _line(text, match.start()),
                                      "kind": kind})
    elif suffix == ".py":
        prefixes = ROUTER_PREFIX.findall(text)
        prefix = prefixes[0].rstrip("/") if len(set(prefixes)) == 1 else ""  # one router per file
        for match in PY_ROUTE.finditer(text):
            verb, path, rest = match.groups()
            if verb in ("route", "api_route"):
                listed = PY_METHODS.search(rest)
                verbs = re.findall(r"[A-Za-z]+", listed.group(1)) if listed else ["GET"]
            else:
                verbs = [verb]
            for method in verbs:
                route(method, prefix + path if prefix and path.startswith("/") else path, match.start(), "python")
        if name == "urls.py":
            for match in DJANGO_PATH.finditer(text):
                route("ANY", "/" + match.group(1).lstrip("^").lstrip("/"), match.start(), "django")
        if (name == "models.py" or "/models/" in f"/{rel}") and "django" in text:
            for match in DJANGO_MODEL.finditer(text):
                if match.group(1) != "Meta" and not NOT_DJANGO_BASES.search(match.group(2)):
                    table(match.group(1), match.start(), "django")
        for match in SQLMODEL_TABLE.finditer(text):
            table(match.group(1), match.start(), "sqlmodel")
        for match in SQLALCHEMY.finditer(text):
            table(match.group(1), match.start(), "sqlalchemy")
        for match in CELERY.finditer(text):
            found["jobs"].append({"name": match.group(1), "file": rel, "line": _line(text, match.start()),
                                  "kind": "celery task"})
        for match in CELERY_BEAT.finditer(text):
            found["jobs"].append({"name": f"{match.group(1)} -> {match.group(2)}", "file": rel,
                                  "line": _line(text, match.start()), "kind": "celery beat schedule"})
    elif suffix == ".go":
        for match in GO_ROUTE.finditer(text):
            verb, explicit, path = match.groups()
            if " " in path:  # Go 1.22 patterns: "GET /path"
                verb, path = path.split(None, 1)
            elif explicit:
                verb = explicit
            route(verb if verb.upper() in METHODS else "ANY", path, match.start(), "go")
    elif suffix in (".java", ".kt"):
        declared = re.search(r"\b(?:class|interface|object)\s+\w+", text)
        body = declared.start() if declared else 0
        prefix = ""
        for match in SPRING_ROUTE.finditer(text):
            if text[text.rfind("\n", 0, match.start()) + 1:match.start()].lstrip().startswith(("*", "//", "/*")):
                continue  # mentioned in a comment
            paths = re.findall(r"\"(/?[^\"]*)\"", match.group(2) or "")
            paths = [p for p in paths if p.startswith("/") or not p] if (match.group(2) or "").strip() else [""]
            if match.start() < body:  # a class-level @RequestMapping prefixes the methods below it
                prefix = (paths[0] if paths else "").rstrip("/")
                continue
            for path in paths or [""]:
                full = (prefix + ("/" + path.lstrip("/") if path.strip("/") else "")) or "/"
                route("ANY" if match.group(1) == "Request" else match.group(1), full, match.start(), "spring")
        for match in JPA_ENTITY.finditer(text):
            named = JPA_TABLE.search(text, match.end(), match.end() + 400)
            owner = re.search(r"\b(?:class|data\s+class)\s+(\w+)", text[match.end():match.end() + 600])
            if named and (not owner or named.start() < match.end() + owner.start()):
                table(named.group(1), match.start(), "jpa")
            elif owner:
                table(owner.group(1), match.start(), "jpa")
        for match in SPRING_SCHEDULED.finditer(text):
            found["jobs"].append({"name": match.group(1), "file": rel, "line": _line(text, match.start()),
                                  "kind": "spring scheduled"})
    elif suffix == ".rb":
        if name == "routes.rb":
            for method, path, offset in _rails_routes(text):
                route(method, path, offset, "rails")
        for match in RUBY_JOB.finditer(text):
            if (match.group(1) or match.group(2)) != "ApplicationJob":
                found["jobs"].append({"name": match.group(1) or match.group(2), "file": rel,
                                      "line": _line(text, match.start()), "kind": "rails job"})
        if name == "schema.rb":
            for match in RAILS_TABLE.finditer(text):
                table(match.group(1), match.start(), "rails")
    elif suffix == ".php":
        for method, path, offset in _laravel_routes(rel, text):
            route(method, path, offset, "laravel")
        for match in LARAVEL_TABLE.finditer(text):
            table(match.group(1), match.start(), "laravel")
        for match in LARAVEL_JOB.finditer(text):
            found["jobs"].append({"name": match.group(1), "file": rel, "line": _line(text, match.start()),
                                  "kind": "laravel job"})
        for match in LARAVEL_SCHEDULE.finditer(text):
            found["jobs"].append({"name": match.group(2).strip().rsplit("\\", 1)[-1], "file": rel,
                                  "line": _line(text, match.start()), "kind": "laravel schedule"})
    elif suffix == ".prisma":
        for match in PRISMA_MODEL.finditer(text):
            table(match.group(1), match.start(), "prisma")
    elif suffix == ".sql":
        for match in SQL_TABLE.finditer(text):
            table(match.group(1), match.start(), "sql")
    elif name == "vercel.json":
        for match in re.finditer(r"\{\s*\"path\"\s*:\s*\"([^\"]+)\"\s*,\s*\"schedule\"\s*:\s*\"([^\"]+)\"", text):
            found["jobs"].append({"name": f"{match.group(1)} ({match.group(2)})", "file": rel,
                                  "line": _line(text, match.start()), "kind": "vercel cron"})
    elif SCHEDULE_FILE.match(name):
        for match in YAML_CRON.finditer(text):
            block = match.group(2)
            cron = re.search(r"^\s+(?:cron|every|schedule):\s*['\"]?([^'\"#\n]+)", block, re.M)
            target = re.search(r"^\s+(?:class|job|task|command):\s*['\"]?([\w:.]+)", block, re.M)
            if cron:
                found["jobs"].append({"name": f"{match.group(1)} -> {target.group(1)}" if target else match.group(1),
                                      "file": rel, "line": _line(text, match.start()), "kind": "cron schedule"})
    elif suffix in (".graphql", ".gql"):
        for block in GRAPHQL_BLOCK.finditer(text):
            operation = block.group(1).lower()
            end = text.find("\n}", block.end())
            body_start = block.end()
            body = text[body_start:end if end != -1 else len(text)]
            depth = 0
            for line_match in re.finditer(r"[^\n]*\n?", body):
                line = line_match.group(0)
                if depth == 0 and (field := GRAPHQL_FIELD.match(line)):
                    found["operations"].append({"kind": f"graphql {operation}", "name": field.group(1), "file": rel,
                                                "line": _line(text, body_start + line_match.start())})
                depth += line.count("(") - line.count(")")
    return found


def scan(repo: Path, engine: Path | str | None = None) -> dict:
    """Every recognized declaration, newest-first order not implied; bounded per kind.

    `engine`: an ontologyex-discover binary (or "auto" to look for one) that reads Python,
    JavaScript/TypeScript and Go from syntax trees; None, or a binary that cannot, uses the patterns.
    """
    repo = repo.resolve()
    result: dict[str, list[dict]] = {"routes": [], "operations": [], "tables": [], "jobs": []}
    total, chosen = 0, []
    for rel in walk(repo):
        path = repo / rel
        if path.is_symlink():
            continue
        size = path.stat().st_size
        limit = MAX_SCHEMA_FILE if Path(rel).suffix.lower() in (".graphql", ".gql", ".prisma", ".sql") else MAX_FILE
        if size > limit or total + size > MAX_TOTAL:
            continue
        total += size
        chosen.append(rel)
    trees, parser = None, {"name": "patterns"}
    if engine is not None:
        import native_view_bridge
        try:
            binary = native_view_bridge.engine_binary("ontologyex-discover") if engine == "auto" else Path(engine)
            native = native_view_bridge.native_surface(repo, [r for r in chosen if r.lower().endswith(TREE_SUFFIXES)],
                                                       binary)
            skipped = set(native["not_parsed"])
            trees = {rel: native["files"].get(rel, {}) for rel in chosen
                     if rel.lower().endswith(TREE_SUFFIXES) and rel not in skipped}
            parser = {"name": "tree-sitter", "languages": native.get("languages", []), "files": len(trees)}
        except native_view_bridge.NativeViewError as exc:
            parser = {"name": "patterns", "note": str(exc)}
    for rel in chosen:
        text = (repo / rel).read_text("utf-8", "replace")
        for kind, rows in scan_file(rel, text, trees.get(rel) if trees is not None else None).items():
            result[kind].extend(rows)
    read = len(chosen)
    # Migrations repeat what a schema declares: keep one row per table name, from a schema
    # (Prisma, Django, ...) before SQL, then the shallowest file.
    seen, tables = set(), []
    for row in sorted(result["tables"], key=lambda r: (r["source"] == "sql", r["file"].count("/"), r["file"], r["line"])):
        key = row["name"].lower()
        if key not in seen:
            seen.add(key)
            tables.append(row)
    if any(row["source"] != "sql" for row in tables):
        for row in tables:  # a schema exists, so a table only migrations mention is likely history
            if row["source"] == "sql" and "migration" in row["file"].lower():
                row["note"] = "only in migrations"
    result["tables"] = sorted(tables, key=lambda r: (r["file"], r["line"]))
    found = {kind: len(rows) for kind, rows in result.items()}
    return {"schema_version": 1, "kind": "ONTOLOGYEX_CODE_SURFACE", "files_read": read, "parser": parser,
            **{kind: rows[:MAX_PER_KIND] for kind, rows in result.items()},
            "found": found, "omitted": {k: n - MAX_PER_KIND for k, n in found.items() if n > MAX_PER_KIND},
            "semantics": "SYNTAX_CANDIDATES_NOT_VERIFIED_ENDPOINTS_OR_TABLES"}


def owners(repo: Path, topology: dict) -> list[tuple[str, str]]:
    """(folder, component) for each app the topology shows: compose build contexts, then manifests."""
    import otel_setup

    ids = {n["id"] for n in topology["nodes"]}
    found = {folder: component for folder, (_, component) in otel_setup._compose_builds(repo, topology).items()
             if component in ids}
    for node in topology["nodes"]:
        if node["kind"] == "source_manifest":
            found.setdefault(PurePosixPath(node["label"]).parent.as_posix(), node["id"])
    return list(found.items())


def attach(topology: dict, repo: Path, apps: list[dict] | None = None, surface: dict | None = None) -> dict:
    """The topology plus a node per route and table (bounded), each under the app that holds it.

    API routes and operations take the map budget before pages, so a large site's
    pages do not crowd out its API.
    """
    repo = repo.resolve()
    surface = surface if surface is not None else scan(repo)
    if apps is None:
        apps = [{"folder": f, "component": c} for f, c in owners(repo, topology)]
    nodes, edges = list(topology["nodes"]), list(topology["edges"])
    evidence, gaps = list(topology["source_evidence"]), list(topology.get("gaps", []))
    ids = {n["id"] for n in nodes}
    evidence_ids = {e["id"] for e in evidence}
    holders = sorted(((a["folder"], a["component"]) for a in apps if a.get("component") in ids),
                    key=lambda fc: -len(fc[0]))
    digests: dict[str, str] = {}

    def owner(rel: str) -> str:
        for folder, component in holders:
            if folder == "." or rel.startswith(folder.rstrip("/") + "/"):
                return component
        return topology["project"]

    def add(kind: str, rel: str, line: int, label: str) -> bool:
        node_id = f"discovery:{kind}:{quote(rel, safe='/-._~')}:{quote(label, safe='-._~/:')}"
        if node_id in ids or len(node_id) > 1024:
            return False
        if rel not in digests:
            digests[rel] = hashlib.sha256((repo / rel).read_bytes()).hexdigest()
        evidence_id = hashlib.sha256(f"{rel}\0{line}\0{kind}\0{label}".encode()).hexdigest()
        if evidence_id not in evidence_ids:
            evidence_ids.add(evidence_id)
            evidence.append({"id": evidence_id, "path": rel, "sha256": digests[rel],
                             "source_kind": "CODE_SYNTAX_EXTRACTED", "authenticity": "UNVERIFIED"})
        ids.add(node_id)
        nodes.append({"id": node_id, "kind": kind, "label": label, "state": "SOURCE_SYNTAX_CANDIDATE",
                      "evidence_id": evidence_id, "source_line": line})
        edges.append({"source": owner(rel), "target": node_id, "relation": "DECLARES",
                      "evidence_id": evidence_id, "state": "SOURCE_SYNTAX_CANDIDATE"})
        return True

    budget = {"api_route": MAX_MAP_PER_KIND, "declared_db_table": MAX_MAP_PER_KIND}
    routes = sorted(surface["routes"], key=lambda r: r["method"] == "PAGE")
    rows = ([("api_route", r["file"], r["line"], f"{r['method']} {r['path']}") for r in routes if r["method"] != "PAGE"] +
            [("api_route", o["file"], o["line"], f"{o['kind']} {o['name']}") for o in surface["operations"]] +
            [("declared_db_table", t["file"], t["line"], t["name"]) for t in surface["tables"]] +
            [("api_route", r["file"], r["line"], f"PAGE {r['path']}") for r in routes if r["method"] == "PAGE"])
    skipped = {"api_route": 0, "declared_db_table": 0}
    for kind, rel, line, label in rows:
        if budget[kind] and add(kind, rel, line, label):
            budget[kind] -= 1
        elif not budget[kind]:
            skipped[kind] += 1
    for kind, count in skipped.items():
        if count:
            gaps.append({"source": "code", "reason": f"{count} more {kind} candidates not drawn (map budget)"})
    coverage = {**topology["coverage"], "api_route_paths_found": len(surface["routes"]) + len(surface["operations"]),
                "db_schema_candidates_found": len(surface["tables"]), "code_files_read": surface["files_read"]}
    return {**topology, "nodes": nodes, "edges": edges, "source_evidence": evidence, "gaps": gaps,
            "coverage": coverage}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--engine", choices=("auto", "python"), default="auto",
                        help="auto: read Python, JS/TS and Go from syntax trees when ontologyex-discover is installed")
    args = parser.parse_args()
    try:
        print(json.dumps(scan(args.repo, "auto" if args.engine == "auto" else None), indent=2))
        return 0
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "ERROR", "error": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
