#!/usr/bin/env python3
"""The outside companies a business runs on, what relies on each, and where it has no second one.

    python suppliers.py --repo REPO [--json]

A supplier is a company the repository says it uses: a vendor SDK in a manifest
(stripe, @sendgrid/mail, boto3), or a vendor's settings named in an example env file
or a compose file (STRIPE_SECRET_KEY: the name only, never a value; real .env files are
never read). Each supplier gets a category and how much the business leans on that
kind of supplier, the lines that show it, and the apps that declare it. In the control
room each becomes an entity that the declaring service depends on, so "what relies on
Stripe" reaches the business actions through confirmed links, and change impact and
agents see suppliers like any other part of the system.

A category with exactly one supplier is marked SINGLE_SUPPLIER: no second provider is
declared in the sources. That is a fact about the sources, not a judgement that a
fallback is needed. Tables and routes the business keeps about its own suppliers
(suppliers, vendors, purchase orders) are listed too. Open-source libraries and
self-run databases are not suppliers and are not listed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re

CRITICALITY = ("CRITICAL", "HIGH", "MEDIUM", "LOW")
CATEGORIES = {
    "payments": "CRITICAL", "banking and finance data": "CRITICAL", "identity and sign-in": "CRITICAL",
    "managed data and jobs": "CRITICAL", "identity verification": "CRITICAL",
    "email and messaging": "HIGH", "cloud and hosting": "HIGH", "AI models": "HIGH", "tax": "HIGH",
    "shipping and logistics": "HIGH", "commerce platform": "HIGH", "accounting": "HIGH",
    "search": "MEDIUM", "maps": "MEDIUM", "CRM and support": "MEDIUM", "media": "MEDIUM",
    "analytics and monitoring": "LOW", "feature flags": "LOW",
}
# (name, category, dependency names or prefixes ending in *, setting-name prefixes)
VENDORS = (
    ("Stripe", "payments", ("stripe", "stripe-*", "@stripe/*", "github.com/stripe/*", "com.stripe:*", "dj-stripe",
                            "laravel/cashier", "stripe/stripe-php"), ("STRIPE_",)),
    ("PayPal", "payments", ("paypal*", "@paypal/*", "paypal/*", "braintree*", "braintree/*"), ("PAYPAL_", "BRAINTREE_")),
    ("Paddle", "payments", ("paddle", "@paddle/*", "laravel/cashier-paddle"), ("PADDLE_",)),
    ("Lemon Squeezy", "payments", ("lemonsqueezy*", "@lemonsqueezy/*"), ("LEMONSQUEEZY_", "LEMON_SQUEEZY_")),
    ("Chargebee", "payments", ("chargebee*",), ("CHARGEBEE_",)),
    ("Recurly", "payments", ("recurly*",), ("RECURLY_",)),
    ("Adyen", "payments", ("adyen*", "@adyen/*"), ("ADYEN_",)),
    ("Square", "payments", ("square", "squareup"), ("SQUARE_",)),
    ("Razorpay", "payments", ("razorpay*",), ("RAZORPAY_",)),
    ("Mollie", "payments", ("mollie*", "@mollie/*", "mollie/*"), ("MOLLIE_",)),
    ("Polar", "payments", ("@polar-sh/*",), ("POLAR_",)),
    ("RevenueCat", "payments", ("@revenuecat/*",), ("REVENUECAT_",)),
    ("Plaid", "banking and finance data", ("plaid*", "@plaid/*", "react-plaid-link", "github.com/plaid/*"), ("PLAID_",)),
    ("Teller", "banking and finance data", ("teller",), ("TELLER_",)),
    ("GoCardless", "banking and finance data", ("gocardless*",), ("GOCARDLESS_",)),
    ("TrueLayer", "banking and finance data", ("truelayer*",), ("TRUELAYER_",)),
    ("MX", "banking and finance data", ("mx-platform-*",), ("MX_CLIENT",)),
    ("Finicity", "banking and finance data", ("@finicity/*",), ("FINICITY_",)),
    ("Alpaca", "banking and finance data", ("alpaca*",), ("ALPACA_", "APCA_")),
    ("Avalara", "tax", ("avalara", "avatax"), ("AVALARA_", "AVATAX_")),
    ("TaxJar", "tax", ("taxjar*",), ("TAXJAR_",)),
    ("Clerk", "identity and sign-in", ("@clerk/*",), ("CLERK_",)),
    ("Auth0", "identity and sign-in", ("auth0", "@auth0/*", "auth0-*"), ("AUTH0_",)),
    ("Supabase", "managed data and jobs", ("supabase", "@supabase/*", "supabase-*"), ("SUPABASE_",)),
    ("Firebase", "managed data and jobs", ("firebase", "firebase-admin", "firebase-*"), ("FIREBASE_",)),
    ("Okta", "identity and sign-in", ("@okta/*",), ("OKTA_",)),
    ("WorkOS", "identity and sign-in", ("@workos-inc/*", "workos"), ("WORKOS_",)),
    ("Kinde", "identity and sign-in", ("@kinde-oss/*",), ("KINDE_",)),
    ("hCaptcha", "identity and sign-in", ("hcaptcha", "@hcaptcha/*"), ("HCAPTCHA_",)),
    ("Persona", "identity verification", ("persona",), ("PERSONA_",)),
    ("Onfido", "identity verification", ("onfido*", "@onfido/*"), ("ONFIDO_",)),
    ("SendGrid", "email and messaging", ("sendgrid*", "@sendgrid/*"), ("SENDGRID_",)),
    ("Postmark", "email and messaging", ("postmark*",), ("POSTMARK_",)),
    ("Resend", "email and messaging", ("resend", "resend/*"), ("RESEND_",)),
    ("Mailgun", "email and messaging", ("mailgun*", "mailgun.js"), ("MAILGUN_",)),
    ("Twilio", "email and messaging", ("twilio*", "twilio/*"), ("TWILIO_",)),
    ("Slack", "email and messaging", ("@slack/*", "slack-sdk", "slack_sdk"), ("SLACK_",)),
    ("Pusher", "email and messaging", ("pusher*", "pusher/*"), ("PUSHER_",)),
    ("Novu", "email and messaging", ("@novu/*",), ("NOVU_",)),
    ("OneSignal", "email and messaging", ("onesignal*",), ("ONESIGNAL_",)),
    ("Mailchimp", "email and messaging", ("@mailchimp/*", "mailchimp-*"), ("MAILCHIMP_",)),
    ("Vonage", "email and messaging", ("@vonage/*",), ("VONAGE_",)),
    ("Svix", "email and messaging", ("svix",), ("SVIX_",)),
    ("OpenAI", "AI models", ("openai", "openai-*", "ruby-openai", "github.com/sashabaranov/go-openai", "openai-php/*"),
     ("OPENAI_",)),
    ("Anthropic", "AI models", ("anthropic", "anthropic-*", "@anthropic-ai/*"), ("ANTHROPIC_",)),
    ("Cohere", "AI models", ("cohere*",), ("COHERE_",)),
    ("Google AI", "AI models", ("@google/generative-ai", "@google/genai", "google-generativeai", "google-genai"),
     ("GEMINI_", "GOOGLE_GENERATIVE_AI")),
    ("Mistral", "AI models", ("mistralai", "@mistralai/*"), ("MISTRAL_",)),
    ("Groq", "AI models", ("groq", "groq-sdk"), ("GROQ_",)),
    ("Replicate", "AI models", ("replicate",), ("REPLICATE_",)),
    ("Amazon Web Services", "cloud and hosting", ("aws-sdk*", "@aws-sdk/*", "boto3", "botocore", "github.com/aws/*",
                                                  "aws/aws-sdk-php", "software.amazon.awssdk:*", "aws-java-sdk-*",
                                                  "league/flysystem-aws-s3-v3"), ("AWS_",)),
    ("Google Cloud", "cloud and hosting", ("@google-cloud/*", "google-cloud-*", "google/cloud-*", "googleapis",
                                           "@googleapis/*"), ("GCP_", "GOOGLE_CLOUD_", "GCLOUD_")),
    ("Microsoft Azure", "cloud and hosting", ("@azure/*", "azure-*"), ("AZURE_",)),
    ("Vercel", "cloud and hosting", ("@vercel/*",), ("VERCEL_",)),
    ("Cloudflare", "cloud and hosting", ("cloudflare", "@cloudflare/*"), ("CLOUDFLARE_", "CF_API")),
    ("Microsoft 365", "cloud and hosting", ("@microsoft/microsoft-graph-client",), ("MS_GRAPH_", "MICROSOFT_GRAPH_")),
    ("Upstash", "managed data and jobs", ("@upstash/*",), ("UPSTASH_",)),
    ("Neon", "managed data and jobs", ("@neondatabase/*",), ("NEON_",)),
    ("PlanetScale", "managed data and jobs", ("@planetscale/*",), ("PLANETSCALE_",)),
    ("Inngest", "managed data and jobs", ("inngest",), ("INNGEST_",)),
    ("Trigger.dev", "managed data and jobs", ("@trigger.dev/*",), ("TRIGGER_SECRET", "TRIGGER_API")),
    ("PostHog", "analytics and monitoring", ("posthog*",), ("POSTHOG_", "NEXT_PUBLIC_POSTHOG")),
    ("Segment", "analytics and monitoring", ("@segment/*", "analytics-node"), ("SEGMENT_",)),
    ("Mixpanel", "analytics and monitoring", ("mixpanel*",), ("MIXPANEL_",)),
    ("Amplitude", "analytics and monitoring", ("@amplitude/*",), ("AMPLITUDE_",)),
    ("Sentry", "analytics and monitoring", ("@sentry/*", "sentry-*", "sentry/*", "github.com/getsentry/*"),
     ("SENTRY_", "NEXT_PUBLIC_SENTRY")),
    ("Datadog", "analytics and monitoring", ("dd-trace", "ddtrace", "datadog*"), ("DD_API", "DATADOG_")),
    ("New Relic", "analytics and monitoring", ("newrelic",), ("NEW_RELIC_",)),
    ("Honeybadger", "analytics and monitoring", ("honeybadger*",), ("HONEYBADGER_",)),
    ("Rollbar", "analytics and monitoring", ("rollbar*",), ("ROLLBAR_",)),
    ("Bugsnag", "analytics and monitoring", ("bugsnag*", "@bugsnag/*", "bugsnag/*"), ("BUGSNAG_",)),
    ("Axiom", "analytics and monitoring", ("@axiomhq/*",), ("AXIOM_",)),
    ("Better Stack", "analytics and monitoring", ("logtail*", "@logtail/*"), ("LOGTAIL_", "BETTERSTACK_")),
    ("HubSpot", "CRM and support", ("@hubspot/*", "hubspot-*"), ("HUBSPOT_",)),
    ("Salesforce", "CRM and support", ("jsforce", "simple-salesforce"), ("SALESFORCE_", "SFDC_")),
    ("Zendesk", "CRM and support", ("node-zendesk", "zenpy"), ("ZENDESK_",)),
    ("Intercom", "CRM and support", ("intercom*", "@intercom/*"), ("INTERCOM_",)),
    ("Shopify", "commerce platform", ("@shopify/*", "shopify*"), ("SHOPIFY_",)),
    ("WooCommerce", "commerce platform", ("@woocommerce/*", "woocommerce*"), ("WOOCOMMERCE_", "WC_CONSUMER")),
    ("Shippo", "shipping and logistics", ("shippo",), ("SHIPPO_",)),
    ("EasyPost", "shipping and logistics", ("easypost", "@easypost/*"), ("EASYPOST_",)),
    ("ShipStation", "shipping and logistics", ("shipstation*",), ("SHIPSTATION_",)),
    ("QuickBooks", "accounting", ("intuit-oauth", "node-quickbooks", "quickbooks*"), ("QUICKBOOKS_", "QBO_")),
    ("Xero", "accounting", ("xero-node", "xero-python"), ("XERO_",)),
    ("Algolia", "search", ("algoliasearch", "@algolia/*"), ("ALGOLIA_", "NEXT_PUBLIC_ALGOLIA")),
    ("Google Maps", "maps", ("@googlemaps/*", "googlemaps"), ("GOOGLE_MAPS_",)),
    ("Mapbox", "maps", ("mapbox-gl", "@mapbox/*"), ("MAPBOX_", "NEXT_PUBLIC_MAPBOX")),
    ("Cloudinary", "media", ("cloudinary*",), ("CLOUDINARY_",)),
    ("Mux", "media", ("@mux/*",), ("MUX_",)),
    ("UploadThing", "media", ("uploadthing", "@uploadthing/*"), ("UPLOADTHING_",)),
    ("LaunchDarkly", "feature flags", ("launchdarkly-*", "@launchdarkly/*"), ("LAUNCHDARKLY_", "LD_SDK")),
)
ENV_EXAMPLES = re.compile(r"(^|/)(\.env\.(example|sample|template|dist|defaults)|example\.env|env\.example|"
                          r"\.env\.local\.example|\.env\.development\.example)$", re.I)
SETTING = re.compile(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]{2,80})\s*[=:]", re.M)
OWN_SUPPLIERS = re.compile(r"(^|[^a-z])(suppliers?|vendors?|purchase[_ -]?orders?|procurement|sourcing|manufacturers?|"
                           r"wholesalers?|distributors?|supply[_ -]?chain)([^a-z]|$)", re.I)
MAX_EVIDENCE = 8
# Manifests of helper scripts, docs, examples and tests are not the product's suppliers.
TOOLING = re.compile(r"(^|/)(scripts?|tools?|docs?|examples?|example-apps|samples?|demos?|tests?|e2e|fixtures?|"
                     r"benchmarks?)/", re.I)


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _matches(pattern: str, name: str) -> bool:
    return name.startswith(pattern[:-1]) if pattern.endswith("*") else name == pattern


def vendor_for_dependency(name: str):
    return next((v for v in VENDORS if any(_matches(p, name.lower()) for p in v[2])), None)


FRAMEWORK_PREFIX = re.compile(r"^(NEXT_PUBLIC_|NEXT_PRIVATE_|NUXT_PUBLIC_|NUXT_|VITE_|REACT_APP_|EXPO_PUBLIC_|PUBLIC_|"
                              r"GATSBY_|VUE_APP_)")


def vendor_for_setting(name: str):
    bare = FRAMEWORK_PREFIX.sub("", name)
    return next((v for v in VENDORS if any(bare.startswith(p) or name.startswith(p) for p in v[3])), None)


def _settings(repo: Path, rel: str) -> list[tuple[str, int]]:
    """Setting names (never values) an example env file or a compose file lists, with their lines."""
    import otel_setup
    text = (repo / rel).read_text("utf-8", "replace")
    if ENV_EXAMPLES.search(rel):
        return [(m.group(1), text.count("\n", 0, m.start()) + 1) for m in SETTING.finditer(text)]
    try:
        doc = otel_setup.load_compose(text.encode())
    except Exception:  # noqa: BLE001 - a compose file the loader refuses names nothing
        return []
    names = []
    for spec in ((doc or {}).get("services") or {}).values() if isinstance(doc, dict) else []:
        env = spec.get("environment") if isinstance(spec, dict) else None
        keys = list(env) if isinstance(env, dict) else [str(e).split("=", 1)[0] for e in env] if isinstance(env, list) else []
        for key in keys:
            if re.fullmatch(r"[A-Z][A-Z0-9_]{2,80}", str(key)):
                at = text.find(str(key))
                names.append((str(key), text.count("\n", 0, at) + 1 if at >= 0 else 1))
    return names


def scan(repo: Path, nodes: list[dict] | None = None) -> dict:
    """The supplier map of a local repository. `nodes` (the system map) adds the business's own supplier records."""
    import discover_repo
    import otel_setup
    import overview
    repo = repo.resolve()
    paths = overview._walk(repo)
    found: dict[str, dict] = {}

    def add(vendor, evidence: dict, library: str | None = None, setting: str | None = None, app: str | None = None):
        name, category = vendor[0], vendor[1]
        row = found.setdefault(name, {"id": f"discovery:supplier:{slug(name)}", "name": name, "category": category,
                                      "criticality": CATEGORIES[category], "evidence": [], "libraries": [],
                                      "settings": [], "apps": []})
        if len(row["evidence"]) < MAX_EVIDENCE and evidence not in row["evidence"]:
            row["evidence"].append(evidence)
        for key, value in (("libraries", library), ("settings", setting), ("apps", app)):
            if value and value not in row[key]:
                row[key].append(value)

    discovery = discover_repo.discover(repo)
    apps = {a["folder"]: a["service"] for a in otel_setup.find_apps(repo, discovery)}
    manifests = [p for p in paths if Path(p).name in ("package.json", "pyproject.toml", "requirements.txt", "go.mod",
                                                         "Cargo.toml", "Gemfile", "composer.json", "pom.xml",
                                                         "build.gradle", "build.gradle.kts")
                 and not TOOLING.search(p)]
    for rel in sorted(manifests, key=lambda r: (r.count("/"), r)):
        runtime = overview._dependency_names(repo, rel) - overview._dev_only(repo, rel)
        app = apps.get(Path(rel).parent.as_posix())
        for library in sorted(runtime):
            vendor = vendor_for_dependency(library)
            if vendor:
                add(vendor, {**overview._dependency_evidence(repo, rel, library), "kind": "dependency"}, library=library,
                    app=app)
    for rel in sorted(p for p in paths if (ENV_EXAMPLES.search(p) or Path(p).name in discover_repo.COMPOSE_FILES)
                      and not TOOLING.search(p)):
        for setting, line in _settings(repo, rel):
            vendor = vendor_for_setting(setting)
            if vendor:
                add(vendor, {"path": rel, "line": line, "text": setting, "kind": "setting name"}, setting=setting)
    suppliers = sorted(found.values(), key=lambda s: (CRITICALITY.index(s["criticality"]), s["category"], s["name"]))
    categories = {}
    for row in suppliers:
        categories.setdefault(row["category"], []).append(row["name"])
    own = [{"id": n["id"], "label": n["label"], "kind": n["kind"]} for n in (nodes or [])
           if n.get("kind") in ("declared_db_table", "api_route", "source_module") and OWN_SUPPLIERS.search(str(n.get("label")))]
    return {"schema_version": 1, "kind": "SUPPLIER_MAP", "suppliers": suppliers,
            "categories": [{"category": c, "criticality": CATEGORIES[c], "suppliers": names,
                            "single_supplier": CATEGORIES[c] in ("CRITICAL", "HIGH") and len(names) == 1}
                           for c, names in sorted(categories.items(), key=lambda kv: (CRITICALITY.index(CATEGORIES[kv[0]]), kv[0]))],
            "own_supplier_records": own[:100],
            "semantics": "DECLARED_IN_MANIFESTS_AND_SETTING_NAMES_NOT_CONTRACTS_OR_SPEND",
            "execution_authorized": False}


def with_suppliers(topology: dict, report: dict, repo: Path) -> dict:
    """The topology plus one node per supplier, each depended on by the service (or project) that declares it."""
    nodes, edges = list(topology["nodes"]), list(topology["edges"])
    evidence = list(topology["source_evidence"])
    by_path = {e["path"]: e["id"] for e in evidence if e.get("path")}
    services = {n["label"]: n["id"] for n in nodes if n["kind"] == "declared_service"}
    project = next((n["id"] for n in nodes if n["kind"] == "project"), None)
    known = {n["id"] for n in nodes}
    for row in report.get("suppliers", []):
        if row["id"] in known:
            continue
        first = row["evidence"][0]
        if first["path"] not in by_path:
            try:
                sha = hashlib.sha256((repo / first["path"]).read_bytes()).hexdigest()
            except OSError:
                continue
            record = hashlib.sha256(f"supplier-evidence:{first['path']}:{sha}".encode()).hexdigest()
            evidence.append({"id": record, "path": first["path"], "sha256": sha, "source_kind": "LOCAL_MANIFEST_BYTES",
                             "authenticity": "UNVERIFIED"})
            by_path[first["path"]] = record
        nodes.append({"id": row["id"], "kind": "external_supplier", "label": row["name"],
                      "evidence_id": by_path[first["path"]], "source_line": first["line"]})
        sources = [services[a] for a in row["apps"] if a in services] or ([project] if project else [])
        for source in sources:
            edges.append({"source": source, "target": row["id"], "relation": "DEPENDS_ON",
                          "evidence_id": by_path[first["path"]]})
    return {**topology, "nodes": nodes, "edges": edges, "source_evidence": evidence}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--json", action="store_true", help="print the full map as JSON")
    args = parser.parse_args()
    try:
        report = scan(args.repo)
    except (ValueError, OSError) as exc:
        print(json.dumps({"status": "ERROR", "error": str(exc)[:300]}))
        return 1
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    for c in report["categories"]:
        flag = "  (single supplier)" if c["single_supplier"] else ""
        print(f"{c['criticality']:<9} {c['category']}: {', '.join(c['suppliers'])}{flag}")
    if report["own_supplier_records"]:
        print("Own supplier records: " + ", ".join(r["label"] for r in report["own_supplier_records"][:10]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
