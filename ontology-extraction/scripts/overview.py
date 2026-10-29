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
import code_surface
import discover_repo
import otel_setup

PLACEHOLDER = "TODO"
MAX_FILES = 50_000
MAX_LIST = 20
MAX_DOCS = 25
COMPOSE_EXTRA = re.compile(r"(^|/)(docker-)?compose\.[\w-]+\.ya?ml$")
SKIP_DIRS = otel_setup.SKIP_DIRS | {"coverage", "__snapshots__", "testdata", "docs/_build"}
MAX_READ = 512 * 1024              # per file, for the content signals below
MAX_READ_TOTAL = 64 * 1024 * 1024
DATASTORES = re.compile(r"(^|/)(postgres|postgis|mysql|mariadb|mongo|redis|valkey|kafka|rabbitmq|elasticsearch|"
                        r"opensearch|clickhouse|cassandra|memcached|minio|nats|zookeeper|localstack|dynamodb|"
                        r"cockroach|timescale|influxdb|neo4j|qdrant|weaviate|milvus|meilisearch|typesense)")
# A name ending in * matches every dependency that starts with it.
INTEGRATIONS = {
    "payments": ("stripe", "@stripe/stripe-js", "@stripe/react-stripe-js", "github.com/stripe/stripe-go*", "braintree",
                 "@paddle/paddle-js", "@paddle/paddle-node-sdk", "paddle", "lemonsqueezy", "@lemonsqueezy/lemonsqueezy.js",
                 "chargebee", "recurly", "adyen", "@adyen/api-library", "square", "paypal", "@paypal/checkout-server-sdk",
                 "razorpay", "mollie", "@mollie/api-client", "avalara", "avatax", "stripe-rails", "laravel/cashier",
                 "dj-stripe", "@polar-sh/sdk", "@revenuecat/purchases-js", "stripe-ruby-mock", "pay"),
    "banking and finance data": ("plaid", "plaid-python", "plaid-node", "react-plaid-link", "@plaid/*", "teller",
                                 "yodlee", "mx-platform-ruby", "mx-platform-node", "gocardless*", "truelayer*",
                                 "@finicity/*", "github.com/plaid/plaid-go*", "alpaca*", "yfinance"),
    "auth": ("next-auth", "@auth/*", "passport", "@clerk/*", "auth0", "@auth0/*", "firebase-admin",
             "@supabase/supabase-js", "@supabase/ssr", "@supabase/auth-helpers-nextjs", "django-allauth", "authlib",
             "pyjwt", "jsonwebtoken", "jose", "python-jose", "keycloak-js", "lucia", "better-auth", "@okta/*", "arctic",
             "@simplewebauthn/*", "oauthlib", "djangorestframework-simplejwt", "social-auth-app-django", "devise",
             "omniauth", "laravel/sanctum", "laravel/passport", "github.com/coreos/go-oidc*", "github.com/golang-jwt/jwt*",
             "github.com/pquerna/otp", "golang.org/x/oauth2", "@workos-inc/node", "@kinde-oss/*", "@hcaptcha/*",
             "hcaptcha", "altcha", "github.com/altcha-org/*", "doorkeeper", "jwt", "rotp", "pundit", "cancancan",
             "rack-attack", "passlib", "pwdlib", "bcrypt", "argon2-cffi", "itsdangerous", "omniauth-*",
             "django-guardian", "casbin", "oso"),
    "email and messaging": ("nodemailer", "@sendgrid/mail", "sendgrid", "postmark", "resend", "mailgun.js", "mailgun",
                            "twilio", "@slack/*", "slack-sdk", "pusher", "@novu/*", "onesignal", "@react-email/*",
                            "react-email", "django-anymail", "@aws-sdk/client-ses", "@aws-sdk/client-sns",
                            "github.com/knadh/smtppool*", "gopkg.in/gomail*", "github.com/wneessen/go-mail",
                            "@mailchimp/*", "mailchimp-marketing", "@vonage/*", "firebase-messaging", "emails",
                            "fastapi-mail", "flask-mail", "postmark-rails", "sendgrid-ruby", "mailgun-ruby",
                            "intercom-rails", "intercom", "intercom-client", "@intercom/*", "octokit"),
    "AI": ("openai", "@anthropic-ai/sdk", "anthropic", "langchain", "@langchain/*", "llama-index", "llamaindex",
           "cohere", "cohere-ai", "@google/generative-ai", "@google/genai", "google-generativeai", "google-genai",
           "@google-cloud/vertexai", "google-cloud-aiplatform", "replicate", "transformers", "ai", "@ai-sdk/*",
           "mistralai", "@mistralai/mistralai", "groq-sdk", "groq", "ollama", "litellm", "github.com/sashabaranov/go-openai",
           "ruby-openai", "anthropic-sdk-ruby", "langchainrb", "openai-ruby"),
    "data and queues": ("@prisma/client", "prisma", "sequelize", "typeorm", "mongoose", "drizzle-orm", "knex", "kysely",
                        "sqlalchemy", "psycopg", "psycopg2", "psycopg2-binary", "asyncpg", "pg", "postgres", "mysql2",
                        "mysqlclient", "pymysql", "redis", "ioredis", "@upstash/*", "kafkajs", "celery", "kombu", "rq",
                        "dramatiq", "bullmq", "bull", "pg-boss", "graphile-worker", "inngest", "@trigger.dev/*",
                        "@temporalio/*", "temporalio", "amqplib", "pika", "elasticsearch", "@elastic/elasticsearch",
                        "pymongo", "motor", "mongodb", "@neondatabase/serverless", "@planetscale/database",
                        "google-cloud-pubsub", "@google-cloud/pubsub", "sidekiq", "github.com/jackc/pgx*",
                        "github.com/lib/pq", "github.com/jmoiron/sqlx", "gorm.io/gorm", "github.com/redis/go-redis*",
                        "github.com/go-redis/redis*", "github.com/go-sql-driver/mysql", "better-sqlite3", "sqlite3"),
    "cloud": ("aws-sdk", "@aws-sdk/*", "boto3", "github.com/aws/aws-sdk-go*", "@google-cloud/*", "google-cloud-*",
              "@azure/*", "azure-*", "firebase", "@vercel/blob", "@vercel/kv", "@vercel/edge-config", "cloudinary",
              "minio", "github.com/minio/minio-go*", "github.com/rhnvrm/simples3", "@cloudflare/*", "cloudflare",
              "django-storages", "aws-sdk-*", "google-cloud-*", "azure-storage-*"),
    "analytics and monitoring": ("posthog-js", "posthog-node", "posthog", "@segment/*", "analytics-node", "mixpanel",
                                 "@amplitude/*", "@sentry/*", "sentry-sdk", "github.com/getsentry/sentry-go",
                                 "dd-trace", "ddtrace", "newrelic", "@opentelemetry/*", "opentelemetry-*",
                                 "go.opentelemetry.io/otel*", "@vercel/analytics", "prom-client",
                                 "prometheus-client", "github.com/prometheus/client_golang", "sentry-*",
                                 "skylight", "logtail*", "@logtail/*", "scout_apm", "honeybadger", "rollbar",
                                 "bugsnag", "appsignal", "ahoy_matey", "lograge"),
}
# Libraries that show a datastore even when no compose file runs one.
DRIVERS = {
    "postgres": ("psycopg", "psycopg2", "psycopg2-binary", "asyncpg", "pg", "postgres", "@neondatabase/serverless",
                 "github.com/jackc/pgx*", "github.com/lib/pq", "pg-boss", "graphile-worker"),
    "mysql": ("mysql2", "mysqlclient", "pymysql", "@planetscale/database", "github.com/go-sql-driver/mysql"),
    "mongodb": ("mongoose", "mongodb", "pymongo", "motor"),
    "redis": ("redis", "ioredis", "@upstash/redis", "github.com/redis/go-redis*", "github.com/go-redis/redis*",
              "django-redis", "bullmq", "bull", "rq"),
    "sqlite": ("better-sqlite3", "sqlite3"),
}
# Words that show a file is about what the business itself charges for (in a finance app, "money"
# is also the product's own domain; these come first).
REVENUE = {"price", "pricing", "plan", "trial", "subscription", "checkout", "invoice", "billing", "seat", "coupon",
           "stripe", "paddle", "revenue", "fee", "paywall"}
APP_CONSTRUCTOR = re.compile(r"\bFastAPI\s*\(|\bFlask\s*\(\s*__name__|\bexpress\s*\(\s*\)|\bnew\s+Hono\s*\(|"
                             r"\b[Ff]astify\s*\(|NestFactory\.create|http\.ListenAndServe|\becho\.New\(\)|"
                             r"\bgin\.(?:Default|New)\(\)|\bStarlette\s*\(|uvicorn\.run\s*\(|\bcreateServer\s*\(|"
                             r"get_wsgi_application\(|get_asgi_application\(|class\s+Application\s*<\s*Rails::Application")
CONSTRUCTOR_HINTS = ("FastAPI", "Flask", "express", "Hono", "astify", "NestFactory", "ListenAndServe", "echo.New",
                     "gin.", "Starlette", "uvicorn.run", "createServer", "_application(", "Rails::Application")
APP_FRAMEWORKS = {
    "node": ("next", "express", "fastify", "koa", "@nestjs/core", "@hapi/hapi", "hono", "react-router",
             "@remix-run/node", "@remix-run/react", "nuxt", "@sveltejs/kit", "astro", "@trpc/server", "@ts-rest/core",
             "@apollo/server", "graphql-yoga", "@angular/core", "vue", "svelte", "react", "electron", "expo",
             "react-native"),
    "python": ("django", "flask", "fastapi", "starlette", "aiohttp", "tornado", "falcon", "pyramid", "graphene",
               "strawberry-graphql", "ariadne", "celery", "streamlit"),
    "go": ("github.com/labstack/echo*", "github.com/gin-gonic/gin", "github.com/go-chi/chi*", "github.com/gofiber/fiber*",
           "github.com/gorilla/mux", "google.golang.org/grpc", "connectrpc.com/connect"),
    "ruby": ("rails", "sinatra", "hanami", "sidekiq"),
    "php": ("laravel/framework", "symfony/framework-bundle", "slim/slim"),
    "rust": ("axum", "actix-web", "rocket", "warp", "poem", "tonic"),
}
READ_FIRST = [
    ("entry points", re.compile(r"(^|/)(manage|wsgi|asgi)\.py$|(^|/)asgi/__init__\.py$|(^|/)cmd/[^/]+/main\.go$|"
                                r"^main\.go$|(^|/)Program\.cs$|(^|/)src/main\.rs$|(^|/)config\.ru$|"
                                r"(^|/)(src/)?app/(layout|page)\.(tsx|jsx|ts|js)$|(^|/)(src/)?middleware\.(ts|js)$|"
                                r"(^|/)pages/_app\.(tsx|jsx|ts|js)$|(^|/)app/(root|entry\.server|entry\.client)\.(tsx|jsx|ts|js)$|"
                                r"(^|/)server/(router|main|index|app)\.(ts|js|mjs)$|(^|/)celery(conf)?\.py$")),
    ("routes and APIs", re.compile(r"(^|/)(routes?|controllers?|handlers?|endpoints|resolvers|views)(/|\.py$|\.ts$|\.js$|\.go$)|"
                                   r"(^|/)urls\.py$|\.controller\.ts$|(^|/)pages/api/|(^|/)app/api/.*route\.(ts|js)$|"
                                   r"(^|/)(openapi|swagger)[^/]*\.(ya?ml|json)$|\.proto$|\.graphql$")),
    ("data model", re.compile(r"(^|/)schema\.prisma$|(^|/)models?(/|\.py$|\.ts$|\.rb$|\.go$)|(^|/)entities/|"
                              r"(^|/)schema\.(rb|sql|ts)$|(^|/)migrations?/[^/]+$|\.sql$")),
    ("money", re.compile(r"(^|[/_.-])(billing|payments?|pricing|prices|checkout|subscriptions?|invoices?|stripe|"
                         r"refunds?|orders?|cart|wallets?|payouts?|plans|giftcards?|gift_cards?|discounts?|vouchers?|"
                         r"tax|taxes|ledger|credits?)([/_.-]|$)", re.I)),
    ("users and access", re.compile(r"(^|[/_.-])(auth|authentication|login|signup|signin|register|sessions?|accounts?|"
                                    r"permissions?|roles?|users?|members?|teams?|oauth|sso|tokens?)([/_.-]|$)", re.I)),
    ("background jobs", re.compile(r"(^|/)(tasks?|jobs?|workers?|queues?|cron|schedulers?)(/|\.py$|\.ts$|\.js$|\.go$|\.rb$)",
                                   re.I)),
]
CONFIG = re.compile(r"(^|/)\.env\.(example|sample|template|dist)$|(^|/)settings(/[^/]+)?\.py$|(^|/)(core/)?config\.py$|(^|/)(docker-)?compose(\.[\w-]+)*\.ya?ml$|(^|/)supabase/config\.toml$|"
                    r"(^|/)(vercel|netlify|render|fly|app|railway|now)\.(json|toml|ya?ml)$|(^|/)Procfile$|"
                    r"(^|/)(Dockerfile|Containerfile)$|(^|/)config/[^/]+\.(ya?ml|toml|json|ts|js)$|"
                    r"(^|/)config\.(toml|ya?ml)(\.sample|\.example)?$|(^|/)(next|nuxt|vite|remix)\.config\.(js|mjs|ts)$")
CODE = re.compile(r"\.(py|js|jsx|ts|tsx|mjs|cjs|go|rb|java|kt|cs|php|rs|sql|prisma|proto|graphql|ya?ml|json)$")
TEMPLATES = re.compile(r"\.(erb|haml|slim|vue|svelte|astro|hbs|jinja2?|j2|twig|blade\.php|heex|eex)$")
NOT_SOURCE = re.compile(r"(^|/)(tests?|__tests__|spec|e2e|cypress|playwright|fixtures?|mocks?|__mocks__|stories|"
                        r"storybook|i18n|locales?|translations|generated|__generated__|development|seeds?)(/|$)|\.(test|spec|stories|d|gen)\."
                        r"[jt]sx?$|_pb2\.py$|\.pb\.go$|(^|/)test_[^/]+\.py$|_test\.go$|(^|/)(package-lock|yarn\.lock|pnpm-lock)|"
                        r"\.lock$|(^|/)package\.json$|(^|/)tsconfig[^/]*\.json$", re.I)
# Words that, several at once, show a file is about money or about access.
TOPIC_WORDS = {
    "money": re.compile(r"\b(price|pricing|amount|currency|invoice|billing|payment|payout|refund|charge|checkout|"
                        r"subscription|plan|coupon|discount|voucher|tax|gift_?card|wallet|ledger|seat|trial|"
                        r"stripe|paddle|revenue|fee)s?\b", re.I),
    "users and access": re.compile(r"\b(password|login|logout|sign_?in|sign_?up|session|token|jwt|oauth|permission|"
                                   r"role|authenticat\w*|authoriz\w*|otp|totp|2fa|passkey|webauthn|sso|saml|oidc|"
                                   r"api_?key|invite|member)s?\b", re.I),
}
TOPIC_AREA = {"money": "payments", "users and access": "auth"}


def _matches(entry: str, name: str) -> bool:
    return name.startswith(entry[:-1]) if entry.endswith("*") else name == entry


def _walk(repo: Path) -> list[str]:
    paths, base = [], len(str(repo).rstrip(os.sep)) + 1
    for root, dirs, files in os.walk(repo):
        prefix = root[base:].replace(os.sep, "/")
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")
                         and (f"{prefix}/{d}" if prefix else d) not in SKIP_DIRS)
        for name in sorted(files):
            paths.append(f"{prefix}/{name}" if prefix else name)
            otel_setup.check(len(paths) <= MAX_FILES, f"more than {MAX_FILES} files; point --repo at a smaller folder")
    return paths


def _evidence(repo: Path, rel: str, needle: str | None = None) -> dict:
    text = (repo / rel).read_text("utf-8", "replace")
    line, quote = otel_setup._line(text, needle) if needle else (1, text.splitlines()[0][:200] if text.strip() else "")
    return {"path": rel, "line": line, "text": quote}


def _dependency_evidence(repo: Path, rel: str, library: str) -> dict:
    """The manifest line that declares `library` itself, not one that only contains its name."""
    text = (repo / rel).read_text("utf-8", "replace")
    token = re.compile(r"(?<![\w@/.-])" + re.escape(library) + r"(?![\w.-])", re.I)
    for number, line in enumerate(text.splitlines(), 1):
        if token.search(line) and "// indirect" not in line and not line.lstrip().startswith(("#", "//")):
            return {"path": rel, "line": number, "text": line.strip()[:200]}
    return _evidence(repo, rel, library)


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
            return set(re.findall(r"^\s*(?:require\s+)?([\w.\-/]+)\s+v\d[^\n/]*$", text, re.M))  # not // indirect
        if name == "Gemfile":
            return set(re.findall(r"""^\s*gem\s+['"]([^'"]+)['"]""", text, re.M))
        if name == "composer.json":
            data = json.loads(text)
            return set(data.get("require", {})) | set(data.get("require-dev", {})) if isinstance(data, dict) else set()
    except (ValueError, TypeError):
        pass
    return set()


def _runtime_dependencies(repo: Path, rel: str) -> int | None:
    """How many runtime (not dev) dependencies a package.json or pyproject.toml declares; None for others."""
    name = Path(rel).name
    try:
        text = (repo / rel).read_text("utf-8", "replace")
        if name == "package.json":
            data = json.loads(text)
            return len(data["dependencies"]) if isinstance(data, dict) and isinstance(data.get("dependencies"), dict) else 0
        if name == "pyproject.toml":  # a uv or poetry workspace root often holds only dev groups
            import tomllib
            data = tomllib.loads(text)
            project, poetry = data.get("project", {}), data.get("tool", {}).get("poetry", {})
            deps = project.get("dependencies", []) if isinstance(project.get("dependencies"), list) else []
            poetry_deps = poetry.get("dependencies", {}) if isinstance(poetry.get("dependencies"), dict) else {}
            return len(deps) + len([d for d in poetry_deps if d.lower() != "python"])
    except (ValueError, TypeError, AttributeError):
        return None
    return None


def _dev_only(repo: Path, rel: str) -> set[str]:
    """package.json devDependencies that are not also runtime dependencies (build tools, not services)."""
    if Path(rel).name != "package.json":
        return set()
    try:
        data = json.loads((repo / rel).read_text("utf-8", "replace"))
    except ValueError:
        return set()
    if not isinstance(data, dict):
        return set()
    dev = data.get("devDependencies") if isinstance(data.get("devDependencies"), dict) else {}
    runtime = data.get("dependencies") if isinstance(data.get("dependencies"), dict) else {}
    return {d.lower() for d in dev} - {d.lower() for d in runtime}


def _doc_rank(rel: str) -> int | None:
    """Where a document goes in the reading order; None for files that are not project docs."""
    if not re.search(r"\.(md|mdx|rst|txt)$", rel, re.I) or NOT_SOURCE.search(rel) or re.search(
            r"(^|/)(LICEN[CS]E|COPYING|NOTICE|CODE_OF_CONDUCT|PULL_REQUEST_TEMPLATE|ISSUE_TEMPLATE|requirements)[^/]*$"
            r"|(^|/)\.github/", rel, re.I):
        return None
    name, depth = Path(rel).name.upper(), rel.count("/")
    if depth == 0 and name.startswith("README"):
        return 0
    if depth <= 1 and re.match(r"(ARCHITECTURE|AGENTS|CLAUDE|PRODUCT|OVERVIEW|DESIGN|BUSINESS|DOMAIN)", name):
        return 1
    if re.search(r"(^|/)(adrs?|decisions?|rfcs?)/", rel, re.I):
        return 2
    if depth == 0:
        return 6 if name.startswith(("CHANGELOG", "HISTORY", "RELEASE")) else 3
    if re.search(r"pric|billing|plans?\b|business|architect|overview|polic|enterprise|licens|self-?host|deploy|domain",
                 rel, re.I) and depth <= 6:
        return 4
    if (re.match(r"docs?/", rel) or "/docs/" in rel) and depth <= 4 or name.startswith("README") and depth <= 2:
        return 5
    return None


def _entry_points_from_build(repo: Path, paths: list[str]) -> list[str]:
    """Files a Dockerfile, Procfile or package.json says start the program."""
    found = []
    known = set(paths)
    for rel in paths:
        name = Path(rel).name
        if not re.fullmatch(r"Dockerfile|Containerfile|Procfile(\.\w+)?|package\.json", name) or NOT_SOURCE.search(rel) \
                and name != "package.json":
            continue
        try:
            text = (repo / rel).read_text("utf-8", "replace")[:MAX_READ]
        except OSError:
            continue
        base = Path(rel).parent
        commands = re.findall(r"^\s*(?:CMD|ENTRYPOINT)\s+(.+)$", text, re.M | re.I) if name != "package.json" else []
        if name.startswith("Procfile"):
            commands = re.findall(r"^\w[\w-]*:\s*(.+)$", text, re.M)
        if name == "package.json":
            try:
                data = json.loads(text)
            except ValueError:
                continue
            scripts = data.get("scripts") if isinstance(data, dict) and isinstance(data.get("scripts"), dict) else {}
            commands = [v for k, v in scripts.items() if k in ("start", "serve") and isinstance(v, str)]
        for command in commands:
            for module in re.findall(r"\b([A-Za-z_][\w.]*):(?:application|app|create_app\(\))\b", command):
                for candidate in (module.replace(".", "/") + ".py", module.replace(".", "/") + "/__init__.py"):
                    for prefix in (base, Path(".")):
                        path = (prefix / candidate).as_posix().lstrip("./")
                        if path in known:
                            found.append(path)
            for script in re.findall(r"([\w./-]+\.(?:py|js|mjs|cjs|ts))\b", command):
                path = (base / script).as_posix().lstrip("./")
                if path in known:
                    found.append(path)
        if commands and name != "package.json":
            found.append(rel)
    return list(dict.fromkeys(found))


def _topic_scores(repo: Path, paths: list[str], libraries: dict[str, list[str]]) -> dict[str, dict[str, tuple]]:
    """For money and for users and access: file -> (imports a library for it, revenue words, distinct topic
    words, total); for "app": files that construct the web application."""
    scores: dict[str, dict[str, tuple]] = {label: {} for label in (*TOPIC_WORDS, "app")}
    imports = {}
    for label, area in TOPIC_AREA.items():
        names = [re.escape(lib.rstrip("*")) for lib in libraries.get(area, [])]
        python = [re.escape(lib.replace("-", "_")) for lib in libraries.get(area, []) if "/" not in lib and "@" not in lib]
        parts = []
        if names:
            parts.append(r"""['"](?:""" + "|".join(names) + r""")(?:/[^'"]*)?['"]""")
        if python:
            parts.append(r"^\s*(?:from|import)\s+(?:" + "|".join(python) + r")\b")
        imports[label] = re.compile("|".join(parts), re.M) if parts else None
    budget = MAX_READ_TOTAL
    for rel in paths:
        if not (CODE.search(rel) or TEMPLATES.search(rel)) or NOT_SOURCE.search(rel) or re.search(
                r"\.(ya?ml)$|(^|/)(migrations?|migrate)/", rel):
            continue
        try:
            size = (repo / rel).stat().st_size
            if not 0 < size <= MAX_READ or size > budget:
                continue
            budget -= size
            text = (repo / rel).read_text("utf-8", "replace")
        except OSError:
            continue
        for label, words in TOPIC_WORDS.items():
            hits = [m.group(1).lower().replace("_", "") for m in words.finditer(text)]
            uses = len(imports[label].findall(text)) if imports[label] else 0
            if hits or uses:
                scores[label][rel] = (uses, len(set(hits) & REVENUE) if label == "money" else 0, len(set(hits)),
                                      len(hits))
        if any(hint in text for hint in CONSTRUCTOR_HINTS) and APP_CONSTRUCTOR.search(text):
            scores["app"][rel] = (1,)
    return scores


def _ranked_topic(label: str, pattern: re.Pattern, scores: dict[str, tuple]) -> list[str]:
    """Files that use a library for the topic, or say a lot about it, or are named for it and say something."""
    def keep(rel: str) -> bool:
        uses, revenue, distinct, total = scores[rel]
        named = bool(pattern.search(rel))
        return uses > 0 or revenue >= 3 or distinct >= 4 and total >= 8 or named and distinct >= 2
    picked = [rel for rel in scores if keep(rel)]
    return sorted(picked, key=lambda rel: (-min(scores[rel][0], 3), -scores[rel][1], -scores[rel][2],
                                           -min(scores[rel][3], 60), rel.count("/"), rel))


def scan(repo: Path) -> dict:
    repo = repo.resolve()
    otel_setup.check(repo.is_dir(), f"{repo} is not a directory")
    paths = _walk(repo)
    discovery = discover_repo.discover(repo)

    services, datastores, unread = [], [], []
    # Discovery reads the standard compose names; overrides and examples (compose.override.yml,
    # compose.example.yml, docker-compose.prod.yml) often hold the services that matter too.
    compose_files = sorted({e["path"] for e in discovery["source_evidence"]
                            if Path(e["path"]).name in discover_repo.COMPOSE_FILES} |
                           {p for p in paths if COMPOSE_EXTRA.search(p) and not NOT_SOURCE.search(p)},
                           key=lambda rel: (Path(rel).name not in discover_repo.COMPOSE_FILES,  # base file first
                                            bool(re.search(r"dev|test|example|local|ci|override", rel, re.I)),
                                            rel.count("/"), rel))
    for rel in compose_files:
        try:
            doc = otel_setup.load_compose((repo / rel).read_bytes())
        except (ValueError, OSError, otel_setup.yaml.YAMLError) as exc:
            unread.append({"path": rel, "reason": str(exc)[:200]})
            continue
        services_in = doc.get("services") if isinstance(doc, dict) else None
        for name, spec in sorted(services_in.items() if isinstance(services_in, dict) else []):
            folder = Path(rel).parent
            if any(r["name"] == str(name) and Path(r["compose"]).parent == folder for r in services + datastores):
                continue  # an override of a service the base file in this folder declares
            image = spec.get("image") if isinstance(spec, dict) else None
            row = {"name": str(name), "image": image if type(image) is str else None, "compose": rel,
                   "evidence": _evidence(repo, rel, f"{name}:")}
            (datastores if type(image) is str and DATASTORES.search(image.lower()) else services).append(row)

    manifests = [p for p in paths if Path(p).name in ("package.json", "pyproject.toml", "requirements.txt", "go.mod",
                                                         "Cargo.toml", "Gemfile", "composer.json")]
    dependencies = {rel: _dependency_names(repo, rel) for rel in manifests}
    dev_only = {rel: _dev_only(repo, rel) for rel in manifests}
    integrations: dict[str, list[dict]] = {}
    for rel in sorted(manifests, key=lambda rel: (bool(NOT_SOURCE.search(rel) or re.search(r"(^|/)(scripts?|tools?|docs?)/",
                                                                                         rel)), rel.count("/"), rel)):
        for area, entries in INTEGRATIONS.items():
            for library in sorted(dependencies[rel] - dev_only[rel]):
                entry = next((e for e in entries if _matches(e, library)), None)
                hits = integrations.setdefault(area, [])
                if entry is None or any(x["library"] == library or library in x.get("also", ()) for x in hits):
                    continue
                family = next((x for x in hits if entry.endswith("*") and x["entry"] == entry), None)
                if family:  # one row per family (@aws-sdk/*, opentelemetry-*), the rest named under it
                    family.setdefault("also", []).append(library)
                else:
                    hits.append({"library": library, "entry": entry,
                                 "evidence": _dependency_evidence(repo, rel, library)})
    integrations = {area: [{k: v for k, v in hit.items() if k != "entry"} for hit in hits]
                    for area, hits in integrations.items() if hits}

    apps = []
    for app in otel_setup.find_apps(repo, discovery):
        folder_manifests = [rel for rel in manifests if Path(rel).parent.as_posix() == app["folder"]]
        deps = set().union(*(dependencies[rel] for rel in folder_manifests)) if folder_manifests else set()
        frameworks = []
        for entry in APP_FRAMEWORKS.get(app["language"], ()):
            for library in sorted(deps):
                rel = next((m for m in folder_manifests if library in dependencies[m]), None)
                if _matches(entry, library) and rel and not any(f["name"] == library for f in frameworks):
                    frameworks.append({"name": library, "evidence": _dependency_evidence(repo, rel, library)})
        runtime = _runtime_dependencies(repo, app["evidence"]["path"])
        apps.append({**{k: app[k] for k in ("folder", "language", "service", "evidence", "status")},
                     "frameworks": frameworks,
                     **({"role": "tooling only: no runtime dependencies or framework"}
                        if runtime == 0 and not frameworks else {})})

    # A tooling-only package.json at the root should not take the project's name from the real app.
    for app in apps:
        if app.get("role"):
            freed = app["service"]
            app["service"] = f"{freed} (tooling)"
            twin = next((a for a in apps if not a.get("role") and a["service"] == f"{freed}-2"), None)
            if twin:
                twin["service"] = freed
    code = code_surface.scan(repo)
    present = {m.group(2) for d in datastores if (m := DATASTORES.search(d["image"].lower()))}
    present |= {"postgres" for x in present if x in ("postgis", "timescale", "cockroach")}
    present |= {"redis" for x in present if x == "valkey"} | {"mongodb" for x in present if x == "mongo"}
    present |= {"mysql" for x in present if x == "mariadb"}
    for engine, drivers in DRIVERS.items():
        if engine in present:
            continue
        for rel in manifests:
            library = next((d for d in sorted(dependencies[rel]) if any(_matches(e, d) for e in drivers)), None)
            if library:
                datastores.append({"name": engine, "image": None, "inferred_from": f"the {library} library",
                                   "evidence": _dependency_evidence(repo, rel, library)})
                present.add(engine)
                break
    for rel in paths:
        if rel.endswith("schema.prisma") and not present & {"postgres", "mysql", "sqlite", "mongodb"}:
            text = (repo / rel).read_text("utf-8", "replace")
            match = re.search(r'provider\s*=\s*"(postgresql|mysql|sqlite|mongodb|cockroachdb|sqlserver)"', text)
            if match:
                engine = {"postgresql": "postgres", "cockroachdb": "postgres"}.get(match.group(1), match.group(1))
                if engine not in present:
                    line = text[:match.start()].count("\n") + 1
                    datastores.append({"name": engine, "image": None, "inferred_from": "the Prisma datasource",
                                       "evidence": {"path": rel, "line": line, "text": match.group(0)}})
                    present.add(engine)
        if rel.endswith("supabase/config.toml") and "postgres" not in present:
            datastores.append({"name": "postgres", "image": None, "inferred_from": "the Supabase project config",
                               "evidence": _evidence(repo, rel, "[db]")})
            present.add("postgres")

    by_file: dict[str, dict[str, int]] = {}
    for kind, rows in (("routes", code["routes"] + code["operations"]), ("tables", code["tables"]),
                       ("jobs", code["jobs"])):
        for row in rows:
            counts = by_file.setdefault(kind, {})
            counts[row["file"]] = counts.get(row["file"], 0) + 1
    libraries = {area: [hit["library"] for hit in hits] for area, hits in integrations.items()}
    topics = _topic_scores(repo, paths, libraries)
    nonempty = {p for p in paths if (repo / p).is_file() and (repo / p).stat().st_size > 0}
    read_first: dict[str, list[str]] = {}
    for label, pattern in READ_FIRST:
        if label in topics:
            ranked = [p for p in _ranked_topic(label, pattern, topics[label])
                      if not re.search(r"(^|/)(routes\.rb|urls\.py|routes\.php)$", p)]
            read_first[label] = ranked[:MAX_LIST]
            continue
        hits = [p for p in paths if p in nonempty and CODE.search(p) and pattern.search(p) and not NOT_SOURCE.search(p)]
        if label == "entry points":
            started = [p for p in _entry_points_from_build(repo, paths) if p in nonempty]
            # Files that construct the application (FastAPI(), express(), Rails::Application, ...).
            built = sorted((p for p in topics["app"] if not re.search(r"/(routes?|routers?|handlers?|controllers?)/", p)),
                           key=lambda p: (p.count("/"), p))
            named = [p for p in paths if p in nonempty and pattern.search(p) and not NOT_SOURCE.search(p)]
            generic = [p for p in paths if p in nonempty and not NOT_SOURCE.search(p) and re.search(
                r"(^|/)(main|index|server|app)\.(py|js|ts|mjs|go|rb)$", p) and (p.count("/") <= 1 or re.match(
                    r"(src|server|backend|api|apps/[^/]+(/src)?)/[^/]+$", p))]
            read_first[label] = list(dict.fromkeys(started + built + named + generic))[:MAX_LIST]
            continue
        # Files that declare the most routes, tables or jobs come first; then names that suggest the topic.
        declared = by_file.get({"routes and APIs": "routes", "data model": "tables",
                                "background jobs": "jobs"}.get(label, ""), {})
        declared = {f: n for f, n in declared.items() if not NOT_SOURCE.search(f)}
        ranked = sorted(declared, key=lambda f: (-declared[f], f))
        # Then names that suggest the topic, larger files (more of the logic) first.
        rest = sorted((p for p in hits if p not in declared),
                      key=lambda p: (-min((repo / p).stat().st_size // 4096, 8), p.count("/"), p))
        read_first[label] = (ranked + rest)[:MAX_LIST]
    deploys = [n["id"].split(":", 2)[2].rstrip(":") for n in discovery["nodes"] if n["kind"] == "workflow"
               and re.search(r"deploy|release|publish|prod|cd\b", f"{n['id']} {n.get('label', '')}", re.I)]
    read_first["configuration"] = (sorted((p for p in paths if p in nonempty and CONFIG.search(p)
                                           and not NOT_SOURCE.search(p)), key=lambda p: (p.count("/"), p))
                                   + deploys)[:MAX_LIST]
    # Agent and editor notes live in dot folders the walk skips; they are often the best architecture docs.
    notes = sorted(p.relative_to(repo).as_posix() for pattern in (".cursor/rules/*.md*", ".github/copilot-instructions.md")
                   for p in repo.glob(pattern) if p.is_file() and not p.is_symlink() and p.stat().st_size > 0)
    ranked_docs = sorted([(rank, p) for p in paths if p in nonempty and (rank := _doc_rank(p)) is not None]
                         + [(1, p) for p in notes], key=lambda x: (x[0], x[1].count("/"), x[1]))
    docs = [p for _, p in ranked_docs][:MAX_DOCS]
    languages: dict[str, int] = {}
    for p in paths:
        suffix = Path(p).suffix.lower()
        if suffix in (".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rb", ".java", ".kt", ".cs", ".php", ".rs", ".swift",
                      ".sql", ".prisma", ".graphql", ".vue", ".svelte"):
            languages[suffix] = languages.get(suffix, 0) + 1
    return {"schema_version": 1, "kind": "ONTOLOGYEX_REPOSITORY_SCAN", "repo": str(repo), "name": repo.name,
            "commit": business_xray.git_head(repo), "files": len(paths),
            "languages": dict(sorted(languages.items(), key=lambda kv: -kv[1])),
            "apps": apps, "services": services, "datastores": datastores, "integrations": integrations,
            **({"compose_unread": unread} if unread else {}),
            "ci_workflows": sum(n["kind"] == "workflow" for n in discovery["nodes"]),
            "code": {"found": code["found"],
                     "routes": [f"{r['method']} {r['path']}  ({r['file']}:{r['line']})" for r in sorted(
                         code["routes"], key=lambda r: r["method"] == "PAGE")[:60]],
                     "operations": [f"{o['kind']} {o['name']}  ({o['file']}:{o['line']})" for o in code["operations"][:40]],
                     "tables": [f"{t['name']}  ({t['file']}:{t['line']}{', ' + t['note'] if t.get('note') else ''})"
                                for t in code["tables"][:80]],
                     "jobs": [f"{j['kind']} {j['name']}  ({j['file']}:{j['line']})" for j in code["jobs"][:30]],
                     "full_list": f"python {Path(code_surface.__file__).resolve()} --repo {repo}",
                     "semantics": code["semantics"]}, "docs": docs, "read_first": read_first,
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
        if app.get("role"):
            continue  # tooling only; it runs nothing
        ev = _cite(evidence, app["evidence"]["path"], app["evidence"]["line"], app["service"])
        frameworks = ", ".join(f["name"] for f in app["frameworks"]) or "no framework detected"
        systems.append({"id": f"app-{len(systems) + 1}", "name": app["service"], "basis": "observed_in_source",
                        "kind": "application", "evidence": [ev],
                        "description": f"{app['language']} app in {app['folder']} ({frameworks})"})
    rows = result["services"] + result["datastores"]
    app_names = {s["name"] for s in systems}
    for row in rows:
        if row["name"] in app_names:
            continue  # the app this compose service builds
        name = row["name"]
        if sum(r["name"] == name for r in rows) > 1 and row.get("compose") != next(
                r.get("compose") for r in rows if r["name"] == name):
            name = f"{name} ({row['compose']})"  # the same name in another compose file
        ev = _cite(evidence, row["evidence"]["path"], row["evidence"]["line"], name)
        if row.get("inferred_from"):
            description = f"{row['name']} database, inferred from {row['inferred_from']}; no compose file runs it here"
        else:
            description = f"compose service in {row['compose']}{' running ' + row['image'] if row['image'] else ''}"
        systems.append({"id": f"svc-{len(systems) + 1}", "name": name,
                        "basis": "derived_from_source" if row.get("inferred_from") else "observed_in_source",
                        "kind": "datastore" if row in result["datastores"] else "service", "evidence": [ev],
                        "description": description})
    shown = {row["inferred_from"].split()[1] for row in result["datastores"] if row.get("inferred_from", "").startswith("the ")}
    for area, hits in result["integrations"].items():
        for hit in hits:
            if hit["library"] in shown:
                continue  # already a datastore above (postgres inferred from psycopg)
            ev = _cite(evidence, hit["evidence"]["path"], hit["evidence"]["line"], hit["library"])
            systems.append({"id": f"ext-{len(systems) + 1}", "name": hit["library"], "basis": "observed_in_source",
                            "kind": f"external: {area}", "evidence": [ev],
                            "description": f"{area} library declared in {hit['evidence']['path']}"
                                           + (f" (with {', '.join(hit['also'][:6])}{', ...' if len(hit['also']) > 6 else ''})"
                                              if hit.get("also") else "")
                                           + "; what the code uses it for is not traced yet"})
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", result["name"]).strip("-") or "repo"
    return {"schema_version": 1, "id": f"local/{slug}", "name": result["name"],
            "purpose": f"{PLACEHOLDER}: one sentence on what this business or project does, and for whom",
            "stage": f"{PLACEHOLDER}: idea, prototype, live product, ... as the sources show it",
            "audiences": [], "value": [], "money_flows": [], "journeys": [], "systems": systems[:100],
            "evidence": evidence[:200], "risks": [], "unknowns": [], "improvements": []}


def _placeholders(value, where="blueprint") -> list[str]:
    if isinstance(value, str):
        return [where] if value.lstrip().startswith(PLACEHOLDER) else []
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


def _citation_check(ev: dict) -> str:
    """The first and last cited line, so a range that is off at either end shows."""
    lines = ev["quote"].splitlines() or [""]
    if ev["start"] == ev["end"] or len(lines) == 1:
        return f"{ev['id']} {ev['path']}:{ev['start']}  {lines[0].strip()[:100]}"
    return (f"{ev['id']} {ev['path']}:{ev['start']}-{ev['end']}  {lines[0].strip()[:70]}  ...  "
            f"{lines[-1].strip()[:70]}")


def summary(result: dict) -> dict:
    """What the scan found, in a screenful; scan.json has the rest."""
    return {"apps": [f"{a['service']} ({a['language']} in {a['folder']}"
                     f"{': ' + ', '.join(f['name'] for f in a['frameworks']) if a['frameworks'] else ''})"
                     + (" [tooling only]" if a.get("role") else "") for a in result["apps"]],
            "services": [s["name"] for s in result["services"]],
            "datastores": [d["name"] + (f" ({d['image']})" if d.get("image") else f" (inferred from {d['inferred_from']})")
                           for d in result["datastores"]],
            "integrations": {area: [h["library"] for h in hits] for area, hits in result["integrations"].items()},
            "code_found": result["code"]["found"], "docs": result["docs"][:8],
            "read_first": {label: paths[:5] for label, paths in result["read_first"].items()}}


def build(repo: Path, blueprint: Path, out: Path, json_out: Path | None = None, github: str | None = None) -> dict:
    """Check the blueprint against the checkout and write the page.

    With `github` (OWNER/NAME), each citation links to its lines on GitHub at the
    checkout's commit instead of to the local file, for a page that is shared.
    """
    project = business_xray.read(blueprint)
    left = _placeholders(project)
    otel_setup.check(not left, f"fill in or remove the {PLACEHOLDER} placeholders first: {', '.join(left[:8])}")
    compiled = business_xray.compile_blueprints([project], None, repo)
    if github is not None:
        otel_setup.check(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", github) is not None,
                         "--github takes OWNER/NAME")
        for ev in compiled["projects"][0]["evidence"]:
            if ev.get("source") == business_xray.LOCAL:
                otel_setup.check(ev.get("commit"), "--github needs the checkout to be a git repository")
                ev["repo"] = github
                ev["url"] = business_xray.evidence_url(ev)
    template = Path(business_xray.__file__).with_name("business_xray_template.html")
    _write(out, business_xray.render_html(compiled, template))
    if json_out:
        _write(json_out, json.dumps(compiled, indent=2, ensure_ascii=False) + "\n")
    evidence = compiled["projects"][0]["evidence"]
    return {"status": "WRITTEN", "page": str(out), "open": out.resolve().as_uri(), **compiled["totals"],
            "citations": len(evidence),
            # The first cited line of each, so an off-by-a-few range shows at a glance.
            "check_citations": [_citation_check(e) for e in evidence if e.get("source") == business_xray.LOCAL],
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
    b.add_argument("--github", metavar="OWNER/NAME",
                   help="link citations to GitHub at the checkout's commit (for a page you share)")
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
            print(json.dumps({"status": "WRITTEN", "written": written,
                              "kept": [] if str(blueprint) in written else [str(blueprint)],
                              "found": summary(result),
                              "next": f"Read the docs and the read_first files listed in {args.out / 'scan.json'} "
                                      f"(full lists, routes, tables, jobs), fill in {blueprint}, then run: "
                                      f"overview.py build --repo {args.repo} --blueprint {blueprint} "
                                      f"--out {args.out / 'business.html'}. Tell the user {args.out} was "
                                      "created; they can delete it or add it to .gitignore."}, indent=2))
        else:
            print(json.dumps(build(args.repo, args.blueprint, args.out, args.json_out, args.github), indent=2))
        return 0
    except (otel_setup.SetupError, business_xray.BlueprintError, discover_repo.DiscoveryError, OSError,
            ValueError) as exc:
        print(json.dumps({"status": "ERROR", "error": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
