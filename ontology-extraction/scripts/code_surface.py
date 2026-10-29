#!/usr/bin/env python3
"""What a repository's code declares: API routes, data tables, GraphQL and RPC operations, jobs.

    python code_surface.py --repo REPO

A read-only pass beside discover_repo (which reads manifests). It recognizes common
declarations by their syntax, without running or importing anything, and names the
file and line of each:

- routes: Next.js app and pages routers (pages as PAGE), Remix / React Router file
  routes, Express/Fastify/Koa/Hono, Flask, FastAPI, Django urls.py, Go (net/http, chi, gin, echo, gorilla), Spring, Rails, Laravel;
- operations: GraphQL Query/Mutation/Subscription fields, tRPC procedures;
- tables: Prisma models, SQL CREATE TABLE, Django and SQLAlchemy models, Drizzle,
  TypeORM, Mongoose, Sequelize, Rails schema.rb, JPA @Table;
- jobs: Celery tasks and beat schedules, BullMQ queues, node-cron, Inngest functions.

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
PY_ROUTE = re.compile(r"^\s*@\s*[\w.]+\.(route|get|post|put|patch|delete|api_route)\s*\(\s*(?:path\s*=\s*)?"
                      r"[rfub]?['\"](/[^'\"]*)['\"]([^\n]*)", re.M)
PY_METHODS = re.compile(r"methods\s*=\s*[\[(]([^\])]*)[\])]")
DJANGO_PATH = re.compile(r"\b(?:re_)?path\s*\(\s*r?['\"]([^'\"]*)['\"]")
GO_ROUTE = re.compile(r"\.\s*(GET|POST|PUT|PATCH|DELETE|Get|Post|Put|Patch|Delete|Handle|HandleFunc|Any|Method)\s*\(\s*"
                      r"(?:\"(GET|POST|PUT|PATCH|DELETE)\"\s*,\s*)?\"((?:(?:GET|POST|PUT|PATCH|DELETE)\s+)?/[^\"]*)\"")
SPRING_ROUTE = re.compile(r"@(Get|Post|Put|Delete|Patch|Request)Mapping\s*\(\s*(?:(?:value|path)\s*=\s*)?\{?\s*\"(/[^\"]*)\"")
RAILS_ROUTE = re.compile(r"^\s*(get|post|put|patch|delete)\s+['\"](/?[^'\"]+)['\"]|^\s*resources?\s+:(\w+)", re.M)
LARAVEL_ROUTE = re.compile(r"Route::(get|post|put|patch|delete|any)\s*\(\s*['\"]([^'\"]*)['\"]")
GRAPHQL_BLOCK = re.compile(r"^(?:extend\s+)?type\s+(Query|Mutation|Subscription)\b[^{]*\{", re.M)
GRAPHQL_FIELD = re.compile(r"^\s{1,8}(\w+)\s*[(:]")
TRPC = re.compile(r"(?:\bconst\s+(\w+)\s*=|^\s*(\w+)\s*:)\s*(\w*[pP]rocedure)\b", re.M)
PRISMA_MODEL = re.compile(r"^model\s+(\w+)\s*\{", re.M)
SQL_TABLE = re.compile(r"\bCREATE\s+(?:UNLOGGED\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:[\"`]?\w+[\"`]?\.)?[\"`]?(\w+)[\"`]?", re.I)
DRIZZLE = re.compile(r"\b(?:pg|mysql|sqlite)Table\s*\(\s*['\"](\w+)['\"]")
TYPEORM = re.compile(r"@Entity\s*\(\s*(?:['\"](\w+)['\"])?[^)]*\)\s*(?:export\s+)?(?:default\s+)?class\s+(\w+)")
MONGOOSE = re.compile(r"\bmodel\s*(?:<[^>]*>)?\s*\(\s*['\"](\w+)['\"]\s*,")
SEQUELIZE = re.compile(r"\.define\s*\(\s*['\"](\w+)['\"]")
DJANGO_MODEL = re.compile(r"^class\s+(\w+)\s*\(([^)]*Model[^)]*)\)\s*:", re.M)
SQLALCHEMY = re.compile(r"__tablename__\s*=\s*['\"](\w+)['\"]")
RAILS_TABLE = re.compile(r"^\s*create_table\s+['\"](\w+)['\"]", re.M)
JPA_TABLE = re.compile(r"@Table\s*\(\s*name\s*=\s*\"(\w+)\"")
CELERY = re.compile(r"^\s*@\s*(?:[\w.]+\.)?(?:shared_task|task)\b[^\n]*\n(?:\s*@[^\n]*\n)*\s*(?:async\s+)?def\s+(\w+)", re.M)
BULLMQ = re.compile(r"\bnew\s+(?:Queue|Worker)\s*\(\s*['\"]([\w:.-]+)['\"]")
CELERY_BEAT = re.compile(r"['\"]([\w.:-]+)['\"]\s*:\s*\{\s*['\"]task['\"]\s*:\s*['\"]([\w.]+)['\"]")
NODE_CRON = re.compile(r"\bcron\.schedule\s*\(\s*['\"]([^'\"]+)['\"]")
INNGEST = re.compile(r"\.createFunction\s*\(\s*\{\s*id\s*:\s*['\"]([\w:.-]+)['\"]")
REMIX_LOADER = re.compile(r"^export\s+(?:async\s+)?(?:function|const)\s+loader\b", re.M)
REMIX_ACTION = re.compile(r"^export\s+(?:async\s+)?(?:function|const)\s+action\b", re.M)


def _line(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def walk(repo: Path) -> list[str]:
    paths = []
    for root, dirs, files in os.walk(repo):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(files):
            if Path(name).suffix.lower() in CODE:
                paths.append(Path(root, name).relative_to(repo).as_posix())
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


def scan_file(rel: str, text: str) -> dict[str, list[dict]]:
    found: dict[str, list[dict]] = {"routes": [], "operations": [], "tables": [], "jobs": []}
    suffix = Path(rel).suffix.lower()
    name = Path(rel).name

    def route(method: str, path: str, offset: int, framework: str) -> None:
        found["routes"].append({"method": method.upper(), "path": path, "file": rel, "line": _line(text, offset),
                                "framework": framework})

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
            found["operations"].append({"kind": "rpc", "name": match.group(1) or match.group(2), "file": rel,
                                        "line": _line(text, match.start())})
        for pattern, source in ((DRIZZLE, "drizzle"), (MONGOOSE, "mongoose"), (SEQUELIZE, "sequelize")):
            for match in pattern.finditer(text):
                table(match.group(1), match.start(), source)
        for match in TYPEORM.finditer(text):
            table(match.group(1) or match.group(2), match.start(), "typeorm")
        for pattern, kind in ((BULLMQ, "bullmq queue"), (NODE_CRON, "cron schedule"), (INNGEST, "inngest function")):
            for match in pattern.finditer(text):
                found["jobs"].append({"name": match.group(1), "file": rel, "line": _line(text, match.start()),
                                      "kind": kind})
    elif suffix == ".py":
        for match in PY_ROUTE.finditer(text):
            verb, path, rest = match.groups()
            if verb in ("route", "api_route"):
                listed = PY_METHODS.search(rest)
                verbs = re.findall(r"[A-Za-z]+", listed.group(1)) if listed else ["GET"]
            else:
                verbs = [verb]
            for method in verbs:
                route(method, path, match.start(), "python")
        if name == "urls.py":
            for match in DJANGO_PATH.finditer(text):
                route("ANY", "/" + match.group(1).lstrip("^").lstrip("/"), match.start(), "django")
        if name == "models.py" or "/models/" in f"/{rel}":
            for match in DJANGO_MODEL.finditer(text):
                if match.group(1) != "Meta":
                    table(match.group(1), match.start(), "django")
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
        for match in SPRING_ROUTE.finditer(text):
            route("ANY" if match.group(1) == "Request" else match.group(1), match.group(2), match.start(), "spring")
        for match in JPA_TABLE.finditer(text):
            table(match.group(1), match.start(), "jpa")
    elif suffix == ".rb":
        if name == "routes.rb":
            for match in RAILS_ROUTE.finditer(text):
                if match.group(3):
                    route("ANY", "/" + match.group(3), match.start(), "rails")
                else:
                    route(match.group(1), "/" + match.group(2).lstrip("/"), match.start(), "rails")
        if name == "schema.rb":
            for match in RAILS_TABLE.finditer(text):
                table(match.group(1), match.start(), "rails")
    elif suffix == ".php":
        for match in LARAVEL_ROUTE.finditer(text):
            route(match.group(1), "/" + match.group(2).lstrip("/"), match.start(), "laravel")
    elif suffix == ".prisma":
        for match in PRISMA_MODEL.finditer(text):
            table(match.group(1), match.start(), "prisma")
    elif suffix == ".sql":
        for match in SQL_TABLE.finditer(text):
            table(match.group(1), match.start(), "sql")
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


def scan(repo: Path) -> dict:
    """Every recognized declaration, newest-first order not implied; bounded per kind."""
    repo = repo.resolve()
    result: dict[str, list[dict]] = {"routes": [], "operations": [], "tables": [], "jobs": []}
    total, read = 0, 0
    for rel in walk(repo):
        path = repo / rel
        if path.is_symlink():
            continue
        size = path.stat().st_size
        limit = MAX_SCHEMA_FILE if Path(rel).suffix.lower() in (".graphql", ".gql", ".prisma", ".sql") else MAX_FILE
        if size > limit or total + size > MAX_TOTAL:
            continue
        total += size
        read += 1
        for kind, rows in scan_file(rel, path.read_text("utf-8", "replace")).items():
            result[kind].extend(rows)
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
    return {"schema_version": 1, "kind": "ONTOLOGYEX_CODE_SURFACE", "files_read": read,
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
    args = parser.parse_args()
    try:
        print(json.dumps(scan(args.repo), indent=2))
        return 0
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "ERROR", "error": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
