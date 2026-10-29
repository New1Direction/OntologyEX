#!/usr/bin/env python3
"""Compliance packs: what SOC 2, PCI DSS, HIPAA and GDPR ask for, and what the sources show.

    python compliance.py --bundle BUNDLE.json --repo REPO [--pack pci] [--markdown]

Each pack is a short list of real controls (SOC 2 Trust Services Criteria, PCI DSS v4.0
requirements, the HIPAA Security Rule, GDPR articles), each with the evidence a codebase
can show for it: a file, a dependency, a CI step, a table or route, a supplier, an owner.
Every finding cites the line it rests on. A control is SHOWN, PARTLY_SHOWN or
NOT_SHOWN_IN_SOURCES, and what code can never show (policies, contracts, training,
business associate agreements) is listed as NEEDS_A_PERSON instead of guessed.

Whether a pack applies is read from the sources too: PCI DSS when a payments supplier or
card fields appear, HIPAA when health-record names appear, GDPR when personal-data
fields appear; SOC 2 is what business customers usually ask for.

This is a map for preparing an audit, not an audit, a certification or legal advice.
Files are only read; files that commonly hold secrets are never read, and a matched line
that looks like it sets a key, token or password is cited without its text.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
from pathlib import Path, PurePosixPath
import re

ALWAYS_SKIPPED = (".env", ".env.*", "*.env", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*", "id_ed25519*",
                  "*credentials*", "*secret*", ".npmrc", ".pypirc", ".netrc", "*.kdbx")
HIDDEN_READ = {".github", ".gitlab", ".circleci", ".husky"}  # configuration folders that say how code is checked
TEXT = re.compile(r"\.(py|js|jsx|ts|tsx|mjs|cjs|rb|go|rs|java|kt|cs|php|ex|exs|scala|sql|prisma|graphql|ya?ml|toml|"
                  r"json|md|mdx|txt|cfg|ini|conf|tf|hcl|sh|dockerfile)$|(^|/)(Dockerfile|Makefile|Procfile|Gemfile|"
                  r"CODEOWNERS|\.gitignore)$", re.I)
NOT_EVIDENCE = re.compile(r"(^|/)(seeds?|fixtures?|__fixtures__|mocks?|__mocks__|examples?)/|\.seed\.", re.I)
LOG_LINE = re.compile(r"\b(print(ln|f)?\s*\(|console\.(log|warn|error|info)|log(ger)?\.(info|warn|warning|debug|error|"
                      r"print\w*)|lo\.Print|puts\s|echo\s)", re.I)  # a message printed is not evidence of a control
SECRET_LINE = re.compile(r"(key|secret|token|passw|credential|private)\w*\s*[:=]\s*\S", re.I)
MAX_FILES, MAX_BYTES, MAX_READ = 20_000, 400_000, 6_000
MAX_EVIDENCE = 6
TERMS = ("health_data",)  # detectors whose distinct words decide whether a pack applies

WORKFLOWS = r"(^|/)(\.github/workflows/[^/]+\.ya?ml|\.gitlab-ci\.ya?ml|\.circleci/config\.ya?ml|azure-pipelines\.ya?ml|" \
            r"Jenkinsfile|bitbucket-pipelines\.ya?ml)$"
SCHEMAS = r"(\.prisma|schema\.rb|\.sql|schema\.(ts|js|py)|models?\.(py|rb|ts|js)|entity\.ts)$|(^|/)(migrations?|models|entities|db)/"
CODE = r"\.(py|js|jsx|ts|tsx|mjs|rb|go|rs|java|kt|cs|php|ex|exs)$"
MANIFESTS = r"(^|/)(package\.json|requirements[^/]*\.txt|pyproject\.toml|Gemfile|go\.mod|Cargo\.toml|composer\.json|" \
            r"pom\.xml|build\.gradle(\.kts)?)$"
DOCS = r"\.(md|mdx|txt|rst)$"

# name: (what it shows, how it is found). path: a path regex. content: (path regex, line regex). model: computed.
DETECTORS = {
    "security_policy": ("a security policy or how to report vulnerabilities", "path", r"(^|/)SECURITY\.md$|security[-_ ]?policy"),
    "incident_response": ("an incident response plan or runbook", "content",
                          (DOCS, r"incident[- ]response|runbook|post-?mortem|on-?call|incident (commander|severity)")),
    "codeowners": ("CODEOWNERS: named reviewers for each part", "path", r"(^|/)CODEOWNERS$"),
    "pr_template": ("a pull request template", "path", r"pull_request_template\.md$|PULL_REQUEST_TEMPLATE"),
    "ci_tests": ("tests run in CI", "content", (WORKFLOWS, r"\b(pytest|jest|vitest|mocha|go test|cargo test|rspec|"
                                                          r"phpunit|unittest|npm (run )?test|pnpm (run )?test|yarn test|"
                                                          r"mvn (-\S+ )*(test|verify)|gradle\w* test|mix test|dotnet test|playwright|cypress|"
                                                          r"turbo (run )?test|nx (run )?test|make test|bun test|tox|nox)\b")),
    "dependency_updates": ("automated dependency updates", "path",
                           r"(^|/)\.github/dependabot\.ya?ml$|(^|/)renovate\.json5?$|(^|/)\.renovaterc(\.json)?$"),
    "lockfile": ("pinned dependency versions (lockfile)", "path",
                 r"(^|/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|poetry\.lock|Pipfile\.lock|uv\.lock|Cargo\.lock|"
                 r"go\.sum|Gemfile\.lock|composer\.lock|bun\.lockb?)$"),
    "sast": ("static analysis or vulnerability scanning in CI", "content",
             (WORKFLOWS, r"codeql|semgrep|snyk|sonar|bandit|brakeman|gosec|trivy|grype|osv-scanner|npm audit|pip-audit|"
                         r"cargo audit|dependency-check")),
    "secret_scanning": ("secret scanning", "content",
                        (WORKFLOWS + r"|(^|/)\.pre-commit-config\.ya?ml$|(^|/)\.gitleaks\.toml$|(^|/)\.secrets\.baseline$",
                         r"gitleaks|trufflehog|detect-secrets|ggshield|secretlint|\[allowlist\]|\"version\"")),
    "env_ignored": ("real environment files kept out of git", "content", (r"(^|/)\.gitignore$", r"^\s*/?\.env\b")),
    "password_hashing": ("passwords hashed with a slow hash", "content",
                         (MANIFESTS + "|" + CODE + "|" + SCHEMAS, r"\b(bcrypt|argon2|scrypt|pbkdf2|passlib|"
                                                                 r"has_secure_password|password_hash\(|PasswordHasher)\b|"
                                                                 r"gen_salt\(\s*'bf'")),
    "sign_in_supplier": ("sign-in handled by an identity provider", "model", "identity and sign-in"),
    "mfa": ("multi-factor sign-in", "content",
            (CODE + "|" + SCHEMAS, r"\b(mfa|2fa|two[-_ ]?factor|totp|webauthn|passkeys?|otpauth|authenticator app)\b")),
    "access_roles": ("roles or permissions", "content",
                     (SCHEMAS, r"\b(roles?|permissions?|rbac|acl|access_?level|is_?admin|memberships?)\b")),
    "audit_log": ("an audit trail of who did what", "content",
                  (SCHEMAS + "|" + CODE, r"audit[_ -]?(log|trail|event)s?|AuditLog|activity[_ ]log")),
    "monitoring": ("error monitoring, metrics or tracing", "content",
                   (MANIFESTS, r"sentry|datadog|dd-trace|newrelic|new-relic|honeycomb|rollbar|bugsnag|opentelemetry|"
                               r"prom-client|prometheus|elastic-apm|logtail|axiom")),
    "health_observed": ("services report their health to the control room", "model", "health"),
    "transport_security": ("encryption in transit (HTTPS, HSTS, TLS to the database)", "content",
                           (CODE + r"|\.(ya?ml|toml|conf|tf)$", r"Strict-Transport-Security|\bhsts\b|force_ssl|"
                                                                 r"SECURE_SSL_REDIRECT|sslmode=(require|verify)|"
                                                                 r"\bhelmet\b|secure_headers|ssl:\s*true|tls:\s*true")),
    "rate_limiting": ("rate limiting", "content",
                      (MANIFESTS + "|" + CODE, r"rate[-_ ]?limit|ratelimit|throttl|rack-attack|slowapi|express-slow-down")),
    "encryption_at_rest": ("encryption at rest or field encryption", "content",
                           (MANIFESTS + "|" + CODE + r"|\.tf$", r"\bkms\b|client-kms|pgcrypto|fernet|createCipheriv|"
                                                                 r"libsodium|encrypt(ed)?_?(field|column|attribute)|"
                                                                 r"server_side_encryption|storage_encrypted|vault")),
    "backups": ("backups", "content",
                (r"\.(ya?ml|sh|tf|toml)$|(^|/)Makefile$", r"\b(backups?|pg_dump|mysqldump|velero|restic|barman|"
                                                             r"wal-g|point[- ]in[- ]time recovery|backup_retention)\b")),
    "data_export": ("people can export their data", "content",
                    (CODE, r"export[_-]?(my[_-]?)?(data|account|user)|data[_-]?export|download[_-]?(my|your)[_-]?data")),
    "data_deletion": ("people can delete their account or data", "content",
                      (CODE, r"delete[_-]?(my[_-]?)?(account|user|me)\b|deleteAccount|deleteUser|erase[_-]?user|"
                             r"anonymi[sz]e|right[_ ]to[_ ]be[_ ]forgotten")),
    "consent": ("consent is recorded", "content",
                (CODE + "|" + SCHEMAS, r"\bconsent|cookie[-_ ]?(banner|consent)|marketing[_-]?opt|opt[_-]?in\b")),
    "retention": ("data is kept only as long as needed", "content",
                  (CODE + "|" + SCHEMAS, r"data[_ -]?retention|retention[_ -]?(period|policy|days|schedule)|"
                                         r"delete[_-]?after|delete[_-]?expired|expired[_-]?(records|data|rows)|"
                                         r"cleanup[_-]?(expired|old)")),
    "personal_data": ("personal data fields (what to protect)", "content",
                      (SCHEMAS, r"\b(e_?mail|phone(_?number)?|date_?of_?birth|\bdob\b|first_?name|last_?name|full_?name|"
                                r"address(_?line)?|ssn|national_?id|passport|ip_?address)\b")),
    "health_data": ("health-record fields", "content",
                    (SCHEMAS, r"\b(patient|diagnos|prescription|medical|medication|allerg|icd_?10|insurance_?id|"
                              r"health_?record|ehr\b)\w*")),
    "card_data": ("card number or security code fields", "content",
                  (SCHEMAS, r"\b(card_?number|cardNumber|\bpan\b|cvv|cvc|card_?security_?code|track_?data)\b")),
    "payments_supplier": ("payments handled by a payments supplier", "model", "payments"),
    "suppliers_listed": ("the outside suppliers that receive data", "model", "suppliers"),
    "owners_declared": ("named owners for the code", "model", "owners"),
}

# Plain words one of which must appear before a detector's regex runs over a file (most files have none).
NEEDLES = {
    "password_hashing": ("bcrypt", "argon2", "scrypt", "pbkdf2", "passlib", "has_secure_password", "password_hash",
                         "passwordhasher", "gen_salt"),
    "mfa": ("mfa", "2fa", "factor", "totp", "webauthn", "passkey", "otpauth", "authenticator"),
    "audit_log": ("audit", "activity"),
    "transport_security": ("transport-security", "hsts", "force_ssl", "ssl_redirect", "sslmode", "helmet",
                           "secure_headers", "ssl:", "tls:"),
    "rate_limiting": ("rate", "throttl", "rack-attack", "slowapi", "slow-down"),
    "encryption_at_rest": ("kms", "pgcrypto", "fernet", "cipheriv", "sodium", "encrypt", "vault"),
    "data_export": ("export", "download"),
    "data_deletion": ("delete", "erase", "anonymi", "forgotten"),
    "consent": ("consent", "cookie", "opt"),
    "retention": ("retention", "after", "expired", "cleanup"),
}
PERSON = "NEEDS_A_PERSON"
# pack: name, reference, controls: (id, title, checks, what a person must provide). Every check must be found for
# a control to be SHOWN; a check written as a tuple is found when any one of its detectors is.
PACKS = {
    "soc2": ("SOC 2", "AICPA Trust Services Criteria (2017, revised points of focus 2022)", (
        ("CC1", "Governance, people and ethics", (), "Board oversight, hiring checks, a code of conduct, security training."),
        ("CC2.2", "Security responsibilities are communicated", ("security_policy",), "The policies themselves, acknowledged by staff."),
        ("CC3", "Risks are assessed", (), "A yearly risk assessment. The command center ranks risks the code shows; the assessment is a person's."),
        ("CC6.1", "Sign-in and access are controlled", (("password_hashing", "sign_in_supplier"), "access_roles"), "Access reviews: who has access, checked each quarter."),
        ("CC6.1-MFA", "Multi-factor sign-in", ("mfa",), "MFA on admin and cloud accounts (outside the code)."),
        ("CC6.7", "Data is encrypted in transit", ("transport_security",), "TLS settings at the load balancer or host."),
        ("CC6.8", "Unauthorized or vulnerable software is prevented", ("dependency_updates", "lockfile"), None),
        ("CC7.1", "Vulnerabilities are found", ("sast", "secret_scanning"), "Penetration test reports."),
        ("CC7.2", "Systems are monitored", (("monitoring", "health_observed"),), "Who is alerted, and how fast they respond."),
        ("CC7.3", "Incidents are responded to", ("incident_response",), "Records of past incidents and how they were handled."),
        ("CC8.1", "Changes are reviewed and tested", ("ci_tests", "codeowners", "pr_template"), "Branch protection requiring review (set in GitHub, not the code)."),
        ("CC9.2", "Vendors are managed", ("suppliers_listed",), "Vendor security reviews and contracts."),
        ("A1.2", "Backups and recovery", ("backups",), "A tested restore."),
    )),
    "pci": ("PCI DSS", "PCI DSS v4.0", (
        ("3.2", "Card numbers are not stored (or are protected)", ("payments_supplier",), "The supplier's PCI attestation (AOC) and your SAQ."),
        ("4.2", "Card data is encrypted in transit", ("transport_security",), "TLS settings at the edge."),
        ("6.2", "Software is developed securely", ("sast", "codeowners", "ci_tests"), "Secure coding training for developers."),
        ("6.3.3", "Known vulnerabilities are patched", ("dependency_updates", "lockfile"), None),
        ("6.4", "Public web apps are protected", ("rate_limiting", "transport_security"), "A web application firewall, if used."),
        ("7.2", "Access is limited by role", ("access_roles",), "Access reviews."),
        ("8.3", "Strong authentication", (("password_hashing", "sign_in_supplier"),), None),
        ("8.4", "Multi-factor authentication", ("mfa",), "MFA for all access to the cardholder data environment."),
        ("10.2", "Audit logs record who did what", ("audit_log",), "Log retention of at least a year, reviewed daily."),
        ("11.3", "Vulnerability scans", ("sast",), "Quarterly external scans by an approved vendor (ASV)."),
        ("12.8", "Service providers are managed", ("suppliers_listed",), "A list of providers with their PCI responsibilities."),
        ("12.10", "Incident response plan", ("incident_response",), "A tested plan."),
    )),
    "hipaa": ("HIPAA", "HIPAA Security Rule, 45 CFR 164.308 and 164.312", (
        ("164.308(a)(1)", "Risk analysis", (), "A documented risk analysis of where health data lives."),
        ("164.308(a)(6)", "Security incident procedures", ("incident_response",), None),
        ("164.308(a)(7)", "Contingency plan and backups", ("backups",), "A tested restore and a disaster recovery plan."),
        ("164.308(b)(1)", "Business associates", ("suppliers_listed",), "A signed BAA with every supplier that receives health data."),
        ("164.312(a)(1)", "Access control", ("access_roles", ("sign_in_supplier", "password_hashing")), None),
        ("164.312(a)(2)(iv)", "Encryption at rest", ("encryption_at_rest",), "Disk and database encryption settings in the cloud account."),
        ("164.312(b)", "Audit controls", ("audit_log",), None),
        ("164.312(d)", "Person authentication", ("mfa",), None),
        ("164.312(e)(1)", "Transmission security", ("transport_security",), None),
    )),
    "gdpr": ("GDPR", "Regulation (EU) 2016/679", (
        ("Art. 30", "Records of processing: what personal data is kept", ("personal_data",), "The written record of processing activities."),
        ("Art. 7", "Consent is recorded", ("consent",), None),
        ("Art. 15/20", "People can get a copy of their data", ("data_export",), None),
        ("Art. 17", "People can have their data erased", ("data_deletion",), None),
        ("Art. 5(1)(e)", "Data is kept no longer than needed", ("retention",), "The retention schedule."),
        ("Art. 28", "Processors are under contract", ("suppliers_listed",), "A data processing agreement with each supplier."),
        ("Art. 32", "Security of processing", ("encryption_at_rest", "transport_security", "password_hashing"), None),
        ("Art. 33", "Breaches are reported within 72 hours", ("incident_response",), "Who decides, and how the authority is told."),
    )),
}


def _skipped(rel: str) -> bool:
    name = PurePosixPath(rel).name
    return any(fnmatch.fnmatchcase(name, g) or fnmatch.fnmatchcase(rel, g) for g in ALWAYS_SKIPPED)


def _walk(repo: Path) -> list[str]:
    """Repository files, including the configuration folders that say how code is checked."""
    import overview  # here, not at the top: control_room imports this module while overview is still loading
    paths, base = [], len(str(repo).rstrip(os.sep)) + 1
    for root, dirs, files in os.walk(repo):
        prefix = root[base:].replace(os.sep, "/")
        dirs[:] = sorted(d for d in dirs if d not in overview.SKIP_DIRS and (not d.startswith(".") or d in HIDDEN_READ)
                         and (f"{prefix}/{d}" if prefix else d) not in overview.SKIP_DIRS)
        for name in sorted(files):
            paths.append(f"{prefix}/{name}" if prefix else name)
            if len(paths) >= MAX_FILES:
                return paths
    return paths


def scan_repo(repo: Path) -> dict:
    """Every file-based detector's findings, each with path, line and (when safe) the line's text."""
    repo = repo.resolve()
    paths = [p for p in _walk(repo) if not _skipped(p)]
    found: dict[str, list[dict]] = {}
    content = {k: (re.compile(v[2][0], re.I), re.compile(v[2][1], re.I | re.M))  # ^ is a line start in the whole-text check
               for k, v in DETECTORS.items() if v[1] == "content"}
    terms: dict[str, set[str]] = {k: set() for k in TERMS}
    for name, (_, kind, rule) in DETECTORS.items():
        if kind == "path":
            pattern = re.compile(rule)
            found[name] = [{"path": p, "line": 1, "text": None} for p in paths if pattern.search(p)][:MAX_EVIDENCE]
    read = 0
    primary = re.compile(r"\.prisma$|\.sql$|schema\.rb$|(^|/)migrations?/")
    later = re.compile(r"(^|/)(scripts?|tests?|__tests__|e2e|spec|docs?)/|\.(spec|test)\.", re.I)
    for rel in sorted(paths, key=lambda r: (not primary.search(r), bool(later.search(r)), r)):  # product code cited first
        wanted = [k for k, (files, _) in content.items() if files.search(rel) and len(found.get(k, [])) < MAX_EVIDENCE]
        if not wanted or not TEXT.search(rel) or NOT_EVIDENCE.search(rel):
            continue
        try:
            path = repo / rel
            if path.is_symlink() or path.stat().st_size > MAX_BYTES:
                continue
            text = path.read_text("utf-8", "replace")
        except OSError:
            continue
        read += 1
        lines, lower = None, text.lower()
        for key in wanted:
            pattern = content[key][1]
            if key in NEEDLES and not any(n in lower for n in NEEDLES[key]):
                continue
            if not pattern.search(text):
                continue  # most files match nothing: one pass over the text, not one per line
            lines = lines if lines is not None else text.splitlines()
            if key in TERMS:  # every distinct word, not only the line cited
                terms[key].update(m.group(1).lower() for line in lines if not LOG_LINE.search(line)
                                  for m in pattern.finditer(line))
            for number, line in enumerate(lines, 1):
                if pattern.search(line) and not LOG_LINE.search(line):
                    shown = None if SECRET_LINE.search(line) else line.strip()[:160]
                    found.setdefault(key, []).append({"path": rel, "line": number, "text": shown})
                    break  # one line per file is enough to cite
        if read >= MAX_READ:
            break
    return {"found": found, "terms": {k: sorted(v) for k, v in terms.items()}, "files": len(paths), "read": read,
            "truncated": read >= MAX_READ or len(paths) >= MAX_FILES}


def _model(bundle: dict, name: str) -> list[dict]:
    """Findings that come from the model the control room already holds."""
    suppliers = (bundle.get("suppliers") or {}).get("suppliers", [])
    if name in ("sign_in_supplier", "payments_supplier"):
        category = DETECTORS[name][2]
        return [{"path": (s["evidence"] or [{}])[0].get("path"), "line": (s["evidence"] or [{}])[0].get("line"),
                 "text": f"{s['name']} ({s['category']})", "entity": s["id"]}
                for s in suppliers if s["category"] == category][:MAX_EVIDENCE]
    if name == "suppliers_listed":
        return [{"path": None, "line": None, "text": f"{len(suppliers)} suppliers mapped: "
                 + ", ".join(s["name"] for s in suppliers[:8]), "page": "/suppliers"}] if suppliers else []
    if name == "health_observed":
        services = [n for n in bundle["state"].get("nodes", []) if n.get("kind") == "declared_service"]
        live = [n for n in services if n.get("observed_at")]
        return [{"path": None, "line": None, "text": f"{len(live)} of {len(services)} services report health",
                 "page": "/"}] if live else []
    if name == "owners":
        found = bundle.get("ownership") or {}
        return [{"path": found["codeowners"], "line": 1, "text": "declared owners", "page": "/owners"}] \
            if found.get("codeowners") else []
    return []


def report(bundle: dict, scan: dict | None) -> dict:
    """Every pack: whether it applies, and each control's standing with its evidence."""
    if scan is None:
        return {"schema_version": 1, "kind": "COMPLIANCE", "status": "NO_LOCAL_REPOSITORY", "packs": [],
                "execution_authorized": False}
    evidence = dict(scan["found"])
    for name, (_, kind, _) in DETECTORS.items():
        if kind == "model":
            evidence[name] = _model(bundle, name if name != "owners_declared" else "owners")
    suppliers = (bundle.get("suppliers") or {}).get("suppliers", [])
    field = None
    for row in (bundle.get("business") or {}).get("actions", []) + (bundle.get("business") or {}).get("objects", []):
        if re.search(r"patient|clinic|prescri|medical", str(row.get("id")), re.I):
            field = "healthcare"
    applies = {
        "soc2": ("ASKED_BY_CUSTOMERS", "Business customers usually ask for a SOC 2 report before buying."),
        "pci": (("APPLIES", "Payments are taken (" + ", ".join(s["name"] for s in suppliers if s["category"] == "payments")
                 + ").") if evidence.get("payments_supplier") else
                ("APPLIES", "Card fields appear in the data model.") if evidence.get("card_data") else
                ("NOT_INDICATED", "No payments supplier or card fields in the sources.")),
        "hipaa": (("APPLIES", "Health-record fields appear in the data model: " + ", ".join(scan["terms"]["health_data"][:5])
                   + ".") if len(scan.get("terms", {}).get("health_data", [])) >= 2 or field
                  else ("NOT_INDICATED", "No health-record fields in the sources.")),
        "gdpr": (("APPLIES", "Personal data is kept (for people in the EU or UK).") if evidence.get("personal_data")
                 else ("NOT_INDICATED", "No personal-data fields found in the schemas read.")),
    }
    packs = []
    for key, (name, reference, controls) in PACKS.items():
        rows = []
        for control_id, title, checks, person in controls:
            groups = [c if isinstance(c, tuple) else (c,) for c in checks]
            rendered = [[{"detector": d, "shows": DETECTORS[d][0], "found": bool(evidence.get(d)),
                          "evidence": evidence.get(d, [])[:3]} for d in group] for group in groups]
            met = sum(any(c["found"] for c in group) for group in rendered)
            status = (PERSON if not groups else "SHOWN" if met == len(groups) else "PARTLY_SHOWN" if met
                      else "NOT_SHOWN_IN_SOURCES")
            rows.append({"id": control_id, "title": title, "status": status, "person": person,
                         "checks": [{"any_of": group, "found": any(c["found"] for c in group)} for group in rendered]})
        risks = []
        if key == "pci" and evidence.get("card_data"):
            risks.append({"risk": "CARD_FIELDS_IN_SCHEMA", "detail": "Fields named like card numbers or security codes "
                          "appear in the data model; storing them brings the whole system into PCI scope, and a "
                          "security code may never be stored.", "evidence": evidence["card_data"][:3]})
        counts = {s: sum(r["status"] == s for r in rows) for s in ("SHOWN", "PARTLY_SHOWN", "NOT_SHOWN_IN_SOURCES", PERSON)}
        packs.append({"id": key, "name": name, "reference": reference, "applies": applies[key][0],
                      "why": applies[key][1], "controls": rows, "counts": counts, "risks": risks})
    return {"schema_version": 1, "kind": "COMPLIANCE", "status": "OK", "packs": packs, "files_scanned": scan["files"],
            "files_read": scan["read"], "truncated": scan["truncated"],
            "semantics": "EVIDENCE_MAP_NOT_AUDIT_OR_CERTIFICATION", "execution_authorized": False}


def markdown(found: dict, pack: str | None = None) -> str:
    """One or every pack as Markdown, for an auditor or a ticket."""
    out = ["# Compliance evidence map", "",
           "What the sources show for each control, with the lines cited. Not an audit, a certification or legal advice.", ""]
    words = {"SHOWN": "Shown", "PARTLY_SHOWN": "Partly shown", "NOT_SHOWN_IN_SOURCES": "Not shown in the sources",
             PERSON: "Needs a person"}
    for p in found["packs"]:
        if pack and p["id"] != pack:
            continue
        out += [f"## {p['name']} ({p['reference']})", "", f"{p['applies'].replace('_', ' ').capitalize()}: {p['why']}", ""]
        for risk in p["risks"]:
            out.append(f"> **Risk:** {risk['detail']} ({', '.join(e['path'] + ':' + str(e['line']) for e in risk['evidence'])})")
            out.append("")
        for c in p["controls"]:
            out.append(f"- **{c['id']} {c['title']}**: {words[c['status']]}")
            for check in c["checks"]:
                hit = next((x for x in check["any_of"] if x["found"]), None)
                shows = hit["shows"] if hit else " or ".join(x["shows"] for x in check["any_of"])
                cite = ", ".join(f"`{e['path']}:{e['line']}`" if e.get("path") else e.get("text", "")
                                 for e in (hit or {}).get("evidence", []))
                out.append(f"  - {'found' if hit else 'not found'}: {shows}" + (f" ({cite})" if cite else ""))
            if c["person"]:
                out.append(f"  - a person provides: {c['person']}")
        out.append("")
    return "\n".join(out) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bundle", type=Path, required=True, help="a bundle from control_room.py --bundle-only")
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--pack", choices=sorted(PACKS))
    parser.add_argument("--markdown", action="store_true")
    args = parser.parse_args()
    found = report(json.loads(args.bundle.read_text("utf-8")), scan_repo(args.repo))
    if args.markdown:
        print(markdown(found, args.pack))
    else:
        for p in found["packs"]:
            if not args.pack or p["id"] == args.pack:
                print(f"{p['name']:<8} {p['applies']:<18} " + " ".join(f"{k}={v}" for k, v in p["counts"].items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
