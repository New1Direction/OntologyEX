#!/usr/bin/env python3
"""Propose live feeds from what a repository declares, then write the ones a person accepts.

    python feed_setup.py propose --repo REPO
    python feed_setup.py init    --repo REPO --out feeds.json --accept-strong
    python feed_setup.py init    --repo REPO --out feeds.json --accept ID [--accept ID ...]

`propose` reads compose files (and a Prometheus config they mount) and lists
candidate feeds, each with the file and line that declares it:

- a health probe for each service whose `healthcheck` calls an HTTP URL inside
  the container, on the host port that container port is published to (STRONG);
- an OTLP mapping for each service that names itself with OTEL_SERVICE_NAME or
  `service.name` in OTEL_RESOURCE_ATTRIBUTES (STRONG), or that exports traces
  without a name (WEAK: the compose service name is only a guess);
- a Prometheus target for each scrape target that names a compose service, when
  Prometheus itself runs in compose with a published port (STRONG).

What cannot be backed by a declaration is a gap, never a guess. `propose` writes
nothing. `init` writes a feeds config with only the accepted candidates and checks
it with the same validator `feeds.py` uses. Probes go to 127.0.0.1 only.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import tempfile

import yaml

import discover_repo
import feeds
import otel_setup

PROMETHEUS_IMAGE = re.compile(r"(^|/)prometheus(:|@|$)")
HEALTH_URL = re.compile(r"(https?)://(localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\])(?::([0-9]{1,5}))?(/[^\s'\"|;&)<>`]*)?")
DURATION = re.compile(r"^(?:([0-9]+)h)?(?:([0-9]+)m(?!s))?(?:([0-9]+)s)?(?:[0-9]+ms)?$")
TRACE_EXPORT_VARS = ("OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
LOOPBACK_BINDS = ("", "0.0.0.0", "127.0.0.1", "::", "::1", "[::]", "[::1]", "localhost")
MAX_PROMETHEUS_CONFIG = 256 * 1024


class SetupError(ValueError):
    pass


def check(ok: bool, message: str) -> None:
    if not ok:
        raise SetupError(message)


# ---------------------------------------------------------------- YAML with line numbers

def _child(node, key: str):
    if isinstance(node, yaml.MappingNode):
        for k, v in node.value:
            if isinstance(k, yaml.ScalarNode) and k.value == key:
                return v
    return None


def _line(node) -> int:
    return node.start_mark.line + 1 if node is not None else 0


def _env_entry(env_node, name: str):
    """The node that sets `name` in a compose environment (mapping or KEY=VALUE list)."""
    if isinstance(env_node, yaml.MappingNode):
        for k, v in env_node.value:
            if isinstance(k, yaml.ScalarNode) and k.value == name:
                return k
    if isinstance(env_node, yaml.SequenceNode):
        for item in env_node.value:
            if isinstance(item, yaml.ScalarNode) and item.value.startswith(name + "="):
                return item
    return env_node


def _evidence(rel: str, lines: list[str], node) -> dict:
    line = _line(node)
    return {"path": rel, "line": line, "text": lines[line - 1].strip()[:240] if 0 < line <= len(lines) else ""}


# ---------------------------------------------------------------- compose fields

def published_ports(spec) -> dict[int, int]:
    """container port -> host port, for single TCP ports reachable on the host's loopback."""
    mapping: dict[int, int] = {}
    for entry in spec if isinstance(spec, list) else []:
        if isinstance(entry, dict):
            target, published = entry.get("target"), entry.get("published")
            host_ip = str(entry.get("host_ip", ""))
            if entry.get("protocol", "tcp") != "tcp" or host_ip not in LOOPBACK_BINDS:
                continue
            if type(target) is int and str(published).isdigit():
                mapping.setdefault(target, int(published))
            continue
        if type(entry) is not str:
            continue  # a bare number publishes to a random host port
        text, _, protocol = entry.partition("/")
        if protocol not in ("", "tcp"):
            continue
        if text.startswith("["):  # [::1]:8080:80
            address, _, rest = text[1:].partition("]")
            host_ip, parts = f"[{address}]", rest.lstrip(":").split(":")
        else:
            pieces = text.split(":")
            host_ip, parts = (pieces[0], pieces[1:]) if len(pieces) == 3 else ("", pieces)
        if len(parts) != 2 or host_ip not in LOOPBACK_BINDS or not all(p.isdigit() for p in parts):
            continue  # container-only, ranges, or another interface
        host, container = int(parts[0]), int(parts[1])
        if 0 < host <= 65535:
            mapping.setdefault(container, host)
    return mapping


def environment(spec) -> dict[str, str]:
    if isinstance(spec, dict):
        return {str(k): v for k, v in spec.items() if type(v) is str}
    out = {}
    for item in spec if isinstance(spec, list) else []:
        if type(item) is str and "=" in item:
            key, _, value = item.partition("=")
            out[key] = value
    return out


def seconds(value, default: int = 30) -> int:
    """A compose duration ("30s", "1m30s") as feed seconds, within 5..3600."""
    match = DURATION.match(value) if type(value) is str and value else None
    if not match or not any(match.groups()):
        return default
    h, m, s = (int(x or 0) for x in match.groups())
    return max(5, min(3600, h * 3600 + m * 60 + s))


def health_command(test) -> str | None:
    if type(test) is str:
        return test
    if isinstance(test, list) and test and test[0] in ("CMD", "CMD-SHELL"):
        return " ".join(str(x) for x in test[1:])
    return None


def prometheus_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


# ---------------------------------------------------------------- proposal

def propose(repo: Path) -> dict:
    repo = repo.resolve()
    discovery = discover_repo.discover(repo)
    known = {n["id"] for n in discovery["nodes"]}
    compose_files = sorted(e["path"] for e in discovery["source_evidence"]
                           if Path(e["path"]).name in discover_repo.COMPOSE_FILES)
    candidates, gaps, host_ports = [], [], set()
    for rel in compose_files:
        data = (repo / rel).read_bytes()
        try:
            doc = otel_setup.load_compose(data)
            root = yaml.compose(data, Loader=yaml.SafeLoader)
        except (yaml.YAMLError, ValueError) as exc:
            gaps.append({"source": rel, "reason": f"compose file could not be read ({type(exc).__name__})"})
            continue
        lines = data.decode("utf-8", "replace").splitlines()
        services = doc.get("services") if isinstance(doc, dict) else None
        if not isinstance(services, dict):
            continue
        service_nodes = _child(root, "services")
        for name in sorted(services):
            spec = services[name]
            component = discover_repo.canonical_id("service", rel, name)
            if not isinstance(spec, dict) or component not in known:
                continue
            node = _child(service_nodes, name)
            ports = published_ports(spec.get("ports"))
            host_ports.update(ports.values())
            where = f"{rel}:{name}"

            # Health probe from an HTTP healthcheck.
            health = spec.get("healthcheck")
            if isinstance(health, dict) and not health.get("disable"):
                command = health_command(health.get("test"))
                found = HEALTH_URL.search(command or "")
                if command is None:
                    pass  # NONE or no test: nothing declared
                elif not found:
                    gaps.append({"source": where, "reason": "healthcheck is not an HTTP check; add a probe by hand"})
                elif found.group(1) != "http":
                    gaps.append({"source": where, "reason": "healthcheck uses https inside the container; "
                                                         "its certificate will not match 127.0.0.1"})
                else:
                    container_port = int(found.group(3) or 80)
                    path = found.group(4) or "/"
                    if container_port not in ports:
                        gaps.append({"source": where, "reason": f"health URL port {container_port} is not "
                                                             "published to the host"})
                    else:
                        url = f"http://127.0.0.1:{ports[container_port]}{path}"
                        probe = {"component": component, "url": url,
                                 "interval_seconds": seconds(health.get("interval"))}
                        candidates.append({
                            "id": f"probe:{rel}:{name}", "kind": "probe", "strength": "STRONG",
                            "component": component, "entry": probe,
                            "evidence": _evidence(rel, lines, _child(_child(node, "healthcheck"), "test")),
                            "why": f"its healthcheck calls {found.group(0)}; container port {container_port} "
                                   f"is published on host port {ports[container_port]}"})
            elif ports and not isinstance(health, dict):
                gaps.append({"source": where, "reason": "publishes a port but declares no healthcheck; "
                                                     "add one, or a probe by hand"})

            # OTLP mapping from the service's own name for itself.
            env = environment(spec.get("environment"))
            env_node = _child(node, "environment")
            declared = env.get("OTEL_SERVICE_NAME")
            if declared is None:
                for pair in env.get("OTEL_RESOURCE_ATTRIBUTES", "").split(","):
                    key, _, value = pair.partition("=")
                    if key.strip() == "service.name":
                        declared = value.strip()
            if declared is not None and ("${" in declared or "$" in declared):
                gaps.append({"source": where, "reason": "service.name is set at deploy time; map it by hand"})
            elif declared is not None:
                if feeds.SERVICE.fullmatch(declared):
                    candidates.append({
                        "id": f"otlp:{rel}:{name}", "kind": "otlp", "strength": "STRONG", "component": component,
                        "entry": {"service": declared, "component": component},
                        "evidence": _evidence(rel, lines, _env_entry(
                            env_node, "OTEL_SERVICE_NAME" if "OTEL_SERVICE_NAME" in env else "OTEL_RESOURCE_ATTRIBUTES")),
                        "why": f"it names itself {declared!r} in its OpenTelemetry environment"})
                else:
                    gaps.append({"source": where, "reason": "service.name is not a plain name"})
            elif any(var in env for var in TRACE_EXPORT_VARS):
                candidates.append({
                    "id": f"otlp:{rel}:{name}", "kind": "otlp", "strength": "WEAK", "component": component,
                    "entry": {"service": name, "component": component},
                    "evidence": _evidence(rel, lines, _env_entry(
                        env_node, next(v for v in TRACE_EXPORT_VARS if v in env))),
                    "why": f"it exports traces but does not set service.name; {name!r} is a guess "
                           "(its SDK may report unknown_service)"})

            # Prometheus running in compose, and the scrape targets it names.
            image = spec.get("image")
            if type(image) is str and PROMETHEUS_IMAGE.search(image):
                if 9090 not in ports:
                    gaps.append({"source": where, "reason": "Prometheus port 9090 is not published to the host"})
                    continue
                config, config_rel, problem = prometheus_config(repo, rel, spec.get("volumes"))
                if problem:
                    gaps.append({"source": where, "reason": problem})
                    continue
                prom_lines = (repo / config_rel).read_text("utf-8", "replace").splitlines()
                prom_root = yaml.compose((repo / config_rel).read_bytes(), Loader=discover_repo.StrictLoader)
                jobs = config.get("scrape_configs")
                job_nodes = _child(prom_root, "scrape_configs")
                for i, job in enumerate(jobs if isinstance(jobs, list) else []):
                    if not isinstance(job, dict) or type(job.get("job_name")) is not str:
                        continue
                    job_node = job_nodes.value[i] if isinstance(job_nodes, yaml.SequenceNode) else None
                    statics = job.get("static_configs")
                    for static in statics if isinstance(statics, list) else []:
                        targets = static.get("targets") if isinstance(static, dict) else None
                        for target in targets if isinstance(targets, list) else []:
                            host = str(target).rsplit(":", 1)[0]
                            if host not in services or host == name or not isinstance(services[host], dict):
                                continue
                            target_component = discover_repo.canonical_id("service", rel, host)
                            query = (f'min(up{{job="{prometheus_label(job["job_name"])}",'
                                     f'instance="{prometheus_label(str(target))}"}})')
                            candidates.append({
                                "id": f"prometheus:{rel}:{host}:{job['job_name']}", "kind": "prometheus",
                                "strength": "STRONG", "component": target_component,
                                "entry": {"url": f"http://127.0.0.1:{ports[9090]}",
                                          "target": {"component": target_component, "up": query}},
                                "evidence": _evidence(config_rel, prom_lines, job_node),
                                "why": f"Prometheus (service {name!r}) scrapes {target} in job "
                                       f"{job['job_name']!r}; default labels assumed"})
    # Apps that `otel_setup.py run --service NAME` can trace: map the name it gives them.
    named = {c["entry"]["service"] for c in candidates if c["kind"] == "otlp"}
    covered = {c["component"] for c in candidates if c["kind"] == "otlp"}
    try:
        apps = otel_setup.find_apps(repo)
    except (otel_setup.SetupError, OSError) as exc:
        apps = []
        gaps.append({"source": "apps", "reason": f"apps not scanned for tracing: {exc}"})
    for app in apps:
        if app["status"] != "READY" or not app["component"] or app["service"] in named or app["component"] in covered:
            continue
        candidates.append({
            "id": f"otlp-app:{app['folder']}:{app['language']}", "kind": "otlp", "strength": "STRONG",
            "component": app["component"], "entry": {"service": app["service"], "component": app["component"]},
            "evidence": app["evidence"],
            "why": f"started with `otel_setup.py run --service {app['service']}`, its traces carry that name"})
    otlp_port = 4318 if 4318 not in host_ports else 14318
    return {"schema_version": 1, "kind": "FEED_SETUP_PROPOSAL", "repo": str(repo),
            "candidates": candidates, "gaps": gaps, "otlp_port": otlp_port,
            "next": f"python feed_setup.py init --repo {repo} --out feeds.json --accept-strong "
                    "(or --accept ID for each one you want)",
            "semantics": "PROPOSALS_FROM_DECLARATIONS_A_PERSON_CHOOSES_WHAT_TO_MONITOR"}


def prometheus_config(repo: Path, compose_rel: str, volumes) -> tuple[dict, str, str]:
    """The prometheus.yml a Prometheus service mounts from this repository."""
    base = Path(compose_rel).parent
    for volume in volumes if isinstance(volumes, list) else []:
        if isinstance(volume, dict):
            source, target = volume.get("source"), volume.get("target")
        elif type(volume) is str and ":" in volume:
            source, target = volume.split(":")[:2]
        else:
            continue
        if type(source) is not str or type(target) is not str or not source.startswith((".", "/")):
            continue  # named volumes hold no file from the repository
        if target.rstrip("/") == "/etc/prometheus":
            source = source.rstrip("/") + "/prometheus.yml"
        elif not target.endswith(".yml") and not target.endswith(".yaml"):
            continue
        candidate = (repo / base / source).resolve() if not source.startswith("/") else Path(source)
        try:
            rel = candidate.relative_to(repo).as_posix()
        except ValueError:
            return {}, "", "Prometheus config is mounted from outside the repository"
        path = repo / rel
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_PROMETHEUS_CONFIG:
            return {}, "", f"Prometheus config {rel} is missing or not a small regular file"
        try:
            config = discover_repo.load_yaml(path.read_bytes())
        except (yaml.YAMLError, ValueError):
            return {}, "", f"Prometheus config {rel} could not be read"
        if not isinstance(config, dict):
            return {}, "", f"Prometheus config {rel} is not a mapping"
        return config, rel, ""
    return {}, "", "Prometheus runs in compose but mounts no prometheus.yml from the repository"


# ---------------------------------------------------------------- init

def build_config(proposal: dict, accepted: list[dict]) -> dict:
    check(accepted, "nothing accepted: pass --accept ID or --accept-strong")
    config: dict = {"schema_version": 1, "kind": feeds.FEED_KIND, "output": "observations.jsonl",
                    "allowed_hosts": ["127.0.0.1"]}
    probes = [c["entry"] for c in accepted if c["kind"] == "probe"]
    if probes:
        config["probes"] = probes
    proms = [c["entry"] for c in accepted if c["kind"] == "prometheus"]
    if proms:
        urls = {p["url"] for p in proms}
        check(len(urls) == 1, "accepted Prometheus targets from more than one Prometheus; accept one at a time")
        config["prometheus"] = {"url": urls.pop(), "targets": [p["target"] for p in proms]}
    mapped = [c["entry"] for c in accepted if c["kind"] == "otlp"]
    if mapped:
        services: dict[str, str] = {}
        for m in mapped:
            check(services.get(m["service"], m["component"]) == m["component"],
                  f"service.name {m['service']!r} is accepted for two components")
            services[m["service"]] = m["component"]
        config["otlp"] = {"bind": "127.0.0.1", "port": proposal["otlp_port"], "services": services}
    return config


def init(repo: Path, out: Path, accept: list[str], accept_strong: bool, force: bool) -> dict:
    proposal = propose(repo)
    by_id = {c["id"]: c for c in proposal["candidates"]}
    unknown = sorted(set(accept) - set(by_id))
    check(not unknown, f"no such candidate: {', '.join(unknown)} (run propose to list them)")
    chosen = [c for c in proposal["candidates"]
              if c["id"] in accept or (accept_strong and c["strength"] == "STRONG")]
    config = build_config(proposal, chosen)
    check(force or not out.exists(), f"{out} exists; pass --force to replace it")
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=out.parent, prefix=".feeds-", suffix=".json",
                                     delete=False, encoding="utf-8") as handle:
        json.dump(config, handle, indent=2)
    try:
        feeds.load_config(Path(handle.name))  # the same validator `feeds.py run` uses
        os.replace(handle.name, out)
    finally:
        if os.path.exists(handle.name):
            os.unlink(handle.name)
    return {"status": "WRITTEN", "config": str(out), "accepted": [c["id"] for c in chosen],
            "not_accepted": [c["id"] for c in proposal["candidates"] if c not in chosen],
            "gaps": proposal["gaps"],
            "next": f"python operate.py up --repo {proposal['repo']} --feeds {out} [--skill SKILL --links LINKS]"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("propose", help="list candidate feeds with their evidence (writes nothing)")
    p.add_argument("--repo", type=Path, required=True)
    i = commands.add_parser("init", help="write a feeds config with the accepted candidates")
    i.add_argument("--repo", type=Path, required=True)
    i.add_argument("--out", type=Path, required=True)
    i.add_argument("--accept", action="append", default=[], metavar="ID")
    i.add_argument("--accept-strong", action="store_true", help="accept every STRONG candidate")
    i.add_argument("--force", action="store_true")
    args = parser.parse_args()
    try:
        result = (propose(args.repo) if args.command == "propose"
                  else init(args.repo, args.out, args.accept, args.accept_strong, args.force))
        print(json.dumps(result, indent=2))
        return 0
    except (SetupError, feeds.FeedError, discover_repo.DiscoveryError, OSError, ValueError) as exc:
        print(json.dumps({"status": "ERROR", "error": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
