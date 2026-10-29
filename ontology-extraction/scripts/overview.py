#!/usr/bin/env python3
"""An overview of a whole repository or business, for the agent to start from.

    python overview.py scan  --repo REPO --out DIR
    python overview.py build --repo REPO --blueprint DIR/blueprint.json --out DIR/business.html

`scan` reads the repository the way a new engineer would on day one, without running
anything: the apps and their languages and frameworks, the services and datastores
compose declares, the outside services the code depends on (payments, auth, email,
AI, cloud, analytics), CI, the docs, and the files worth reading first (entry points,
routes, data models, money, users). Every finding names the file (and line) it came
from. It writes DIR/scan.json and DIR/blueprint.json, a Business X-Ray draft with the
systems already filled in; the agent reads the read-first files and fills in the
rest: purpose, audiences, value, journeys, money flows, risks, unknowns.

`build` checks the blueprint (every cited line must exist in the repository; no
placeholder left) and writes one offline page. A claim with a citation is still the
author's reading of the code, not a verified fact; what nothing shows stays unknown.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import tempfile

import business_xray
import discover_repo
import otel_setup

PLACEHOLDER = "TODO"
MAX_FILES = 50_000
MAX_LIST = 15
SKIP_DIRS = otel_setup.SKIP_DIRS | {"coverage", "fixtures", "__snapshots__", "testdata", "examples", "docs/_build"}
DATASTORES = re.compile(r"(^|/)(postgres|postgis|mysql|mariadb|mongo|redis|valkey|kafka|rabbitmq|elasticsearch|"
                        r"opensearch|clickhouse|cassandra|memcached|minio|nats|zookeeper|localstack|dynamodb|"
                        r"cockroach|timescale|influxdb|neo4j|qdrant|weaviate|milvus|meilisearch|typesense)")
INTEGRATIONS = {
    "payments": ("stripe", "@stripe/stripe-js", "braintree", "@paddle/paddle-js", "paddle", "lemonsqueezy",
                 "@lemonsqueezy/lemonsqueezy.js", "chargebee", "recurly", "adyen", "@adyen/api-library", "square",
                 "paypal", "@paypal/checkout-server-sdk", "razorpay", "mollie", "@mollie/api-client"),
    "auth": ("next-auth", "@auth/core", "passport", "@clerk/nextjs", "@clerk/clerk-sdk-node", "auth0", "@auth0/nextjs-auth0",
             "firebase-admin", "@supabase/supabase-js", "supabase", "django-allauth", "authlib", "pyjwt", "jsonwebtoken",
             "keycloak-js", "lucia", "better-auth", "@okta/okta-auth-js"),
    "email and messaging": ("nodemailer", "@sendgrid/mail", "sendgrid", "postmark", "resend", "mailgun.js", "mailgun",
                            "twilio", "@slack/web-api", "slack-sdk", "pusher", "@novu/node", "onesignal"),
    "AI": ("openai", "@anthropic-ai/sdk", "anthropic", "langchain", "@langchain/core", "llama-index", "llamaindex",
           "cohere", "cohere-ai", "@google/generative-ai", "google-generativeai", "replicate", "transformers", "ai"),
    "data and queues": ("@prisma/client", "prisma", "sequelize", "typeorm", "mongoose", "drizzle-orm", "knex", "sqlalchemy",
                        "psycopg", "psycopg2", "psycopg2-binary", "pg", "mysql2", "redis", "ioredis", "kafkajs", "celery",
                        "bullmq", "amqplib", "pika", "elasticsearch", "@elastic/elasticsearch", "pymongo"),
    "cloud": ("aws-sdk", "@aws-sdk/client-s3", "boto3", "@google-cloud/storage", "google-cloud-storage", "@azure/storage-blob",
              "firebase", "@vercel/blob", "@vercel/kv", "cloudinary"),
    "analytics and monitoring": ("posthog-js", "posthog-node", "posthog", "@segment/analytics-node", "analytics-node",
                                 "mixpanel", "@amplitude/analytics-browser", "@sentry/node", "@sentry/nextjs",
                                 "@sentry/react", "sentry-sdk", "dd-trace", "newrelic", "@opentelemetry/api"),
}
READ_FIRST = [
    ("entry points", re.compile(r"(^|/)(main|index|server|app|wsgi|asgi|manage)\.(py|js|ts|mjs|go|rb)$|"
                                r"(^|/)cmd/[^/]+/main\.go$|(^|/)Program\.cs$|(^|/)src/main\.rs$|(^|/)config\.ru$")),
    ("routes and APIs", re.compile(r"(^|/)(routes?|controllers?|handlers?|endpoints|resolvers|views)(/|\.py$|\.ts$|\.js$)|"
                                   r"(^|/)urls\.py$|\.controller\.ts$|(^|/)pages/api/|(^|/)app/api/.*route\.(ts|js)$|"
                                   r"(^|/)openapi[^/]*\.(ya?ml|json)$|\.proto$|\.graphql$")),
    ("data model", re.compile(r"(^|/)schema\.prisma$|(^|/)models?(/|\.py$|\.ts$|\.rb$)|(^|/)entities/|"
                              r"(^|/)schema\.(rb|sql|ts)$|(^|/)migrations?/[^/]+$|\.sql$")),
    ("money", re.compile(r"(^|[/_.-])(billing|payments?|pricing|prices|checkout|subscriptions?|invoices?|stripe|"
                         r"refunds?|orders?|cart|wallets?|payouts?|plans)([/_.-]|$)", re.I)),
    ("users and access", re.compile(r"(^|[/_.-])(auth|authentication|login|signup|signin|register|sessions?|accounts?|"
                                    r"permissions?|roles?|users?|members?|teams?|oauth)([/_.-]|$)", re.I)),
]
CODE = re.compile(r"\.(py|js|jsx|ts|tsx|mjs|cjs|go|rb|java|kt|cs|php|rs|sql|prisma|proto|graphql|ya?ml|json)$")


def _walk(repo: Path) -> list[str]:
    paths = []
    for root, dirs, files in os.walk(repo):
        rel_root = Path(root).relative_to(repo).as_posix()
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")
                         and f"{rel_root}/{d}".lstrip("./") not in SKIP_DIRS)
        for name in sorted(files):
            paths.append(Path(root, name).relative_to(repo).as_posix())
            otel_setup.check(len(paths) <= MAX_FILES, f"more than {MAX_FILES} files; point --repo at a smaller folder")
    return paths


def _evidence(repo: Path, rel: str, needle: str | None = None) -> dict:
    text = (repo / rel).read_text("utf-8", "replace")
    line, quote = otel_setup._line(text, needle) if needle else (1, text.splitlines()[0][:200] if text.strip() else "")
    return {"path": rel, "line": line, "text": quote}


def _dependency_names(repo: Path, rel: str) -> set[str]:
    name = Path(rel).name
    text = (repo / rel).read_text("utf-8", "replace")
    try:
        if name in ("package.json", "pyproject.toml", "requirements.txt"):
            return {d.lower() for d in otel_setup._dependencies(rel, text)[1]}
        if name == "Cargo.toml":
            import tomllib
            data = tomllib.loads(text)
            return {d.lower() for section in ("dependencies", "dev-dependencies")
                    for d in (data.get(section) or {}) if isinstance(data.get(section), dict)}
        if name == "go.mod":
            return set(re.findall(r"^\s*(?:require\s+)?([\w.\-/]+)\s+v\d", text, re.M))
        if name == "Gemfile":
            return set(re.findall(r"""^\s*gem\s+['"]([^'"]+)['"]""", text, re.M))
        if name == "composer.json":
            data = json.loads(text)
            return set(data.get("require", {})) | set(data.get("require-dev", {})) if isinstance(data, dict) else set()
    except (ValueError, TypeError):
        pass
    return set()


def scan(repo: Path) -> dict:
    repo = repo.resolve()
    otel_setup.check(repo.is_dir(), f"{repo} is not a directory")
    paths = _walk(repo)
    discovery = discover_repo.discover(repo)
    apps = otel_setup.find_apps(repo)

    services, datastores = [], []
    for evidence in discovery["source_evidence"]:
        rel = evidence["path"]
        if Path(rel).name not in discover_repo.COMPOSE_FILES:
            continue
        doc = discover_repo.load_yaml((repo / rel).read_bytes())
        for name, spec in sorted((doc.get("services") or {}).items() if isinstance(doc, dict) else []):
            image = spec.get("image") if isinstance(spec, dict) else None
            row = {"name": str(name), "image": image if type(image) is str else None,
                   "evidence": _evidence(repo, rel, f"{name}:")}
            (datastores if type(image) is str and DATASTORES.search(image.lower()) else services).append(row)

    manifests = [p for p in paths if Path(p).name in ("package.json", "pyproject.toml", "requirements.txt", "go.mod",
                                                         "Cargo.toml", "Gemfile", "composer.json")]
    integrations: dict[str, list[dict]] = {}
    for rel in manifests:
        names = _dependency_names(repo, rel)
        for area, libraries in INTEGRATIONS.items():
            for library in libraries:
                if library in names and not any(x["library"] == library for x in integrations.get(area, [])):
                    integrations.setdefault(area, []).append(
                        {"library": library, "evidence": _evidence(repo, rel, f'"{library}"' if rel.endswith(".json")
                                                                   else library)})

    read_first: dict[str, list[str]] = {}
    for label, pattern in READ_FIRST:
        hits = [p for p in paths if CODE.search(p) and pattern.search(p) and "test" not in p.lower()]
        read_first[label] = sorted(hits, key=lambda p: (p.count("/"), len(p), p))[:MAX_LIST]
    docs = [p for p in paths if re.search(r"(^|/)(README|ARCHITECTURE|CONTRIBUTING|PRODUCT|OVERVIEW)[^/]*\.(md|rst|txt)$"
                                          r"|^docs?/[^/]+\.(md|mdx|rst)$", p, re.I)]
    docs = sorted(docs, key=lambda p: (p.count("/"), not p.lower().startswith("readme"), p))[:MAX_LIST]
    languages: dict[str, int] = {}
    for p in paths:
        suffix = Path(p).suffix.lower()
        if suffix in (".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rb", ".java", ".kt", ".cs", ".php", ".rs", ".swift"):
            languages[suffix] = languages.get(suffix, 0) + 1
    return {"schema_version": 1, "kind": "ONTOLOGYEX_REPOSITORY_SCAN", "repo": str(repo), "name": repo.name,
            "commit": business_xray.git_head(repo), "files": len(paths),
            "languages": dict(sorted(languages.items(), key=lambda kv: -kv[1])),
            "apps": [{k: a[k] for k in ("folder", "language", "service", "frameworks", "evidence", "status")} for a in apps],
            "services": services, "datastores": datastores, "integrations": integrations,
            "ci_workflows": sum(n["kind"] == "workflow" for n in discovery["nodes"]), "docs": docs, "read_first": read_first,
            "not_assessed": ["what the code does at runtime", "production data, money, and users",
                             "anything outside this repository"],
            "semantics": "WHAT_FILES_DECLARE_NOT_WHAT_RUNS"}


def _cite(evidence: list, rel: str, line: int, label: str) -> str:
    """Add a local citation once; return its ID."""
    for ev in evidence:
        if ev["path"] == rel and ev["start"] == line:
            return ev["id"]
    ev_id = f"e{len(evidence) + 1}"
    evidence.append({"id": ev_id, "repo": business_xray.LOCAL, "path": rel, "start": line, "end": line, "label": label})
    return ev_id


def draft(result: dict) -> dict:
    """A blueprint with what the files show filled in, and placeholders for what only reading can tell."""
    evidence: list[dict] = []
    systems = []
    for app in result["apps"]:
        ev = _cite(evidence, app["evidence"]["path"], app["evidence"]["line"], app["service"])
        frameworks = ", ".join(f["name"] for f in app["frameworks"]) or "no framework detected"
        systems.append({"id": f"app-{len(systems) + 1}", "name": app["service"], "basis": "observed_in_source",
                        "kind": "application", "evidence": [ev],
                        "description": f"{app['language']} app in {app['folder']} ({frameworks})"})
    for row in result["services"] + result["datastores"]:
        if any(s["name"] == row["name"] for s in systems):
            continue
        ev = _cite(evidence, row["evidence"]["path"], row["evidence"]["line"], row["name"])
        systems.append({"id": f"svc-{len(systems) + 1}", "name": row["name"], "basis": "observed_in_source",
                        "kind": "datastore" if row in result["datastores"] else "service", "evidence": [ev],
                        "description": f"compose service{' running ' + row['image'] if row['image'] else ''}"})
    for area, hits in result["integrations"].items():
        for hit in hits:
            ev = _cite(evidence, hit["evidence"]["path"], hit["evidence"]["line"], hit["library"])
            systems.append({"id": f"ext-{len(systems) + 1}", "name": hit["library"], "basis": "observed_in_source",
                            "kind": f"external: {area}", "evidence": [ev],
                            "description": f"{area} dependency; what it is used for is {PLACEHOLDER}"})
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", result["name"]).strip("-") or "repo"
    return {"schema_version": 1, "id": f"local/{slug}", "name": result["name"],
            "purpose": f"{PLACEHOLDER}: one sentence on what this business or project does, and for whom",
            "stage": f"{PLACEHOLDER}: idea, prototype, live product, ... as the sources show it",
            "audiences": [], "value": [], "money_flows": [], "journeys": [], "systems": systems[:100],
            "evidence": evidence[:200], "risks": [], "unknowns": [], "improvements": []}


def _placeholders(value, where="blueprint") -> list[str]:
    if isinstance(value, str):
        return [where] if PLACEHOLDER in value else []
    if isinstance(value, dict):
        return [x for k, v in value.items() for x in _placeholders(v, f"{where}.{k}")]
    if isinstance(value, list):
        return [x for i, v in enumerate(value) for x in _placeholders(v, f"{where}[{i}]")]
    return []


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=f".{path.name}-", delete=False,
                                     encoding="utf-8") as handle:
        handle.write(text)
    os.replace(handle.name, path)


def build(repo: Path, blueprint: Path, out: Path, json_out: Path | None = None) -> dict:
    project = business_xray.read(blueprint)
    left = _placeholders(project)
    otel_setup.check(not left, f"fill in or remove the {PLACEHOLDER} placeholders first: {', '.join(left[:8])}")
    compiled = business_xray.compile_blueprints([project], None, repo)
    template = Path(business_xray.__file__).with_name("business_xray_template.html")
    _write(out, business_xray.render_html(compiled, template))
    if json_out:
        _write(json_out, json.dumps(compiled, indent=2, ensure_ascii=False) + "\n")
    return {"status": "WRITTEN", "page": str(out), **compiled["totals"],
            "citations": len(compiled["projects"][0]["evidence"]),
            "semantics": compiled["trust"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    s = commands.add_parser("scan", help="inventory the repository and write a blueprint draft (reads only)")
    s.add_argument("--repo", type=Path, required=True)
    s.add_argument("--out", type=Path, required=True, help="folder for scan.json and blueprint.json")
    s.add_argument("--force", action="store_true", help="replace an existing blueprint.json with a new draft")
    b = commands.add_parser("build", help="check a blueprint against the repository and write the page")
    b.add_argument("--repo", type=Path, required=True)
    b.add_argument("--blueprint", type=Path, required=True)
    b.add_argument("--out", type=Path, required=True, help="the page to write (.html)")
    b.add_argument("--json-out", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "scan":
            result = scan(args.repo)
            blueprint = args.out / "blueprint.json"
            _write(args.out / "scan.json", json.dumps(result, indent=2) + "\n")
            written = [str(args.out / "scan.json")]
            if args.force or not blueprint.exists():  # an existing one may hold the author's work
                _write(blueprint, json.dumps(draft(result), indent=2) + "\n")
                written.append(str(blueprint))
            print(json.dumps({**result, "written": written,
                              "kept": [] if str(blueprint) in written else [str(blueprint)],
                              "next": f"Read the docs and read_first files, fill in {blueprint}, then run: "
                                      f"overview.py build --repo {args.repo} --blueprint {blueprint} "
                                      f"--out {args.out / 'business.html'}"}, indent=2))
        else:
            print(json.dumps(build(args.repo, args.blueprint, args.out, args.json_out), indent=2))
        return 0
    except (otel_setup.SetupError, business_xray.BlueprintError, discover_repo.DiscoveryError, OSError,
            ValueError) as exc:
        print(json.dumps({"status": "ERROR", "error": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
