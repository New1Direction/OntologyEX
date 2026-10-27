#!/usr/bin/env python3
"""OntologyEX local control room: declared topology + independently supplied status.

Read-only, bound to loopback, no agent execution, no outgoing network calls.
The UI is a local prototype. Input samples are source-reported, not attested.
"""
from __future__ import annotations

import argparse
from collections import defaultdict, deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import RLock
import json
import math
import os
import re
import stat

import discover_repo
import github_discovery

MAX_OBSERVATION_BYTES = 1024 * 1024
MAX_OBSERVATIONS = 4096
MAX_THRESHOLDS_BYTES = 16384
METRICS = frozenset({"queue_lag_seconds", "db_pool_utilization_pct",
                     "error_rate_pct", "p95_latency_ms", "job_lag_seconds"})
PERCENT_METRICS = frozenset({"db_pool_utilization_pct", "error_rate_pct"})
STATUS = frozenset({"ok", "warning", "error"})
SOURCES = frozenset({"otel", "health_probe", "manual_export", "synthetic"})
MAX_AGE = 86400
IDENT_RE = re.compile(r"^discovery:[a-z_]+:[A-Za-z0-9_.~/%:-]{1,600}$")


class RoomError(ValueError):
    pass


def check(condition: bool, message: str) -> None:
    if not condition:
        raise RoomError(message)


def no_duplicates(pairs):
    result = {}
    for key, val in pairs:
        check(key not in result, "duplicate JSON key")
        result[key] = val
    return result


def parse_json(raw: bytes):
    check(len(raw) <= MAX_OBSERVATION_BYTES, "observation input exceeds limit")
    return json.loads(raw.decode("utf-8"), object_pairs_hook=no_duplicates,
                      parse_constant=lambda _: (_ for _ in ()).throw(RoomError("nonfinite number")))


def safe_read(path: Path, max_bytes: int) -> bytes:
    """Read a bounded regular file without following a final-component symlink."""
    check(not path.is_symlink(), "symlinked input refused")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        check(stat.S_ISREG(metadata.st_mode), "expected a regular file")
        check(metadata.st_size <= max_bytes, "input exceeds file budget")
        data = stream.read(max_bytes + 1)
    check(len(data) <= max_bytes, "input exceeds file budget")
    return data


def timestamp(value: str) -> datetime:
    check(type(value) is str and len(value) <= 45 and "T" in value and
          (value.endswith("Z") or re.search(r"[+-][0-9]{2}:[0-9]{2}$", value) is not None),
          "timestamp needs an explicit RFC3339 timezone")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RoomError("invalid observation timestamp") from exc
    check(parsed.tzinfo is not None and parsed.utcoffset() is not None, "ambiguous timezone")
    return parsed.astimezone(timezone.utc)


def valid_local_host(host: str, origin: str | None, port: int) -> bool:
    """Refuse non-loopback Host/Origin headers, including DNS rebinding hosts."""
    if type(host) is not str or port < 1 or port > 65535:
        return False
    allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
    if host.lower() not in allowed:
        return False
    if origin is not None and origin.lower() not in {f"http://{entry}" for entry in allowed}:
        return False
    return True


def read_observations(path: Path | None) -> list[dict]:
    if path is None:
        return []
    raw = safe_read(path, MAX_OBSERVATION_BYTES)
    result = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        check(len(result) < MAX_OBSERVATIONS and len(line) <= 8192, "observation count/line limit")
        row = parse_json(line)
        check(type(row) is dict and set(row) ==
              {"schema_version", "component_id", "observed_at", "status",
               "source", "expires_after_seconds", "metrics"}, "invalid observation shape")
        check(type(row["schema_version"]) is int and row["schema_version"] == 1, "unknown observation schema")
        check(type(row["component_id"]) is str and IDENT_RE.fullmatch(row["component_id"]) is not None,
              "invalid component ID")
        check(type(row["status"]) is str and row["status"] in STATUS and
              type(row["source"]) is str and row["source"] in SOURCES,
              "invalid status/source")
        check(type(row["expires_after_seconds"]) is int and
              1 <= row["expires_after_seconds"] <= MAX_AGE, "invalid freshness TTL")
        metrics = row["metrics"]
        check(type(metrics) is dict and len(metrics) <= len(METRICS) and
              set(metrics).issubset(METRICS), "invalid metric names")
        for metric, value in metrics.items():
            check(type(value) in (float, int) and 0 <= value <= 1e9 and math.isfinite(value),
                  "invalid metric value")
            if metric in PERCENT_METRICS:
                check(value <= 100, "percentage metric exceeds 100")
        row["observed_at"] = timestamp(row["observed_at"]).isoformat().replace("+00:00", "Z")
        result.append(row)
    return result


def read_thresholds(path: Path | None) -> dict:
    if path is None:
        return {}
    data = parse_json(safe_read(path, MAX_THRESHOLDS_BYTES))
    check(type(data) is dict and set(data) == {"schema_version", "warning_above"},
          "invalid thresholds shape")
    check(type(data["schema_version"]) is int and data["schema_version"] == 1,
          "unknown threshold schema")
    rules = data["warning_above"]
    check(type(rules) is dict and set(rules).issubset(METRICS), "invalid threshold names")
    for metric, threshold in rules.items():
        check(type(threshold) in (float, int) and 0 <= threshold <= 1e9 and
              math.isfinite(threshold), "invalid numeric threshold")
        if metric in PERCENT_METRICS:
            check(threshold <= 100, "percentage threshold exceeds 100")
    return rules


def build_state(topology: dict, observations: list[dict], thresholds: dict,
                now: datetime | None = None) -> dict:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    check(type(topology) is dict and topology.get("kind") == "UNREVIEWED_BUSINESS_DISCOVERY_PREVIEW",
          "expected an unreviewed repo discovery preview")
    graph_nodes = topology["nodes"]
    graph_edges = topology["edges"]
    check(len(graph_nodes) <= 4096 and len(graph_edges) <= 16384, "topology budget exceeded")
    by_id = {node["id"]: node for node in graph_nodes}
    check(len(by_id) == len(graph_nodes), "duplicate topology IDs")
    grouped = defaultdict(list)
    gaps = list(topology.get("gaps", []))
    for record in observations:
        component_id = record["component_id"]
        if component_id not in by_id:
            gaps.append({"source": component_id, "reason": "OBSERVATION_UNMAPPED"})
            continue
        t = timestamp(record["observed_at"])
        if t > now:
            gaps.append({"source": component_id, "reason": "FUTURE_OBSERVATION_IGNORED"})
            continue
        grouped[component_id].append(record)
    conflicts = set()
    for component_id, records in grouped.items():
        # Never let JSONL line order decide health when two sources disagree
        # about a component at the exact same timestamp.
        records.sort(key=lambda r: (r["observed_at"], r["source"], r["status"]))
        newest_time = records[-1]["observed_at"]
        newest = [r for r in records if r["observed_at"] == newest_time]
        if len({json.dumps(r, sort_keys=True) for r in newest}) > 1:
            conflicts.add(component_id)
            gaps.append({"source": component_id,
                         "reason": "CONFLICTING_SAME_TIMESTAMP_OBSERVATIONS"})

    nodes = []
    failed = set()
    for node in graph_nodes:
        values = grouped.get(node["id"], [])
        latest = values[-1] if values else None
        state = "unknown"
        leading = []
        if latest is not None:
            age = (now - timestamp(latest["observed_at"])).total_seconds()
            if node["id"] in conflicts:
                state = "unknown"
            elif age > latest["expires_after_seconds"]:
                state = "stale"
            else:
                state = latest["status"]
                if len(values) >= 2:
                    prior = values[-2]
                    prior_age = (now - timestamp(prior["observed_at"])).total_seconds()
                    if (prior_age <= prior["expires_after_seconds"] and
                            prior["source"] == latest["source"]):
                        for metric, threshold in sorted(thresholds.items()):
                            if (metric in prior["metrics"] and metric in latest["metrics"] and
                                    prior["metrics"][metric] > threshold and
                                    latest["metrics"][metric] > threshold):
                                leading.append({
                                    "metric": metric, "threshold": threshold,
                                    "current": latest["metrics"][metric],
                                    "previous": prior["metrics"][metric],
                                    "rule": "TWO_CONSECUTIVE_FRESH_BREACHES"})
                if state == "ok" and leading:
                    state = "warning"
        if state == "error":
            failed.add(node["id"])
        nodes.append({
            "id": node["id"], "kind": node["kind"], "label": node["label"],
            "status": state,
            "source_status": ("conflict" if node["id"] in conflicts else
                              latest["status"] if latest else None),
            "observed_at": latest["observed_at"] if latest else None,
            "expires_after_seconds": latest["expires_after_seconds"] if latest else None,
            "observation_source": latest["source"] if latest else None,
            "source_authenticity": "UNATTESTED_LOCAL_INPUT" if latest else "UNKNOWN",
            "metrics": latest["metrics"] if latest else {},
            "leading_indicators": leading,
            "evidence_id": node.get("evidence_id"),
            "potentially_impacted_by": [],
        })

    reverse = defaultdict(set)
    for edge in graph_edges:
        check(edge["source"] in by_id and edge["target"] in by_id,
              "topology contains dangling edge")
        # Directed DEPENDS_ON from consumer to dependency.
        if edge["relation"] == "DEPENDS_ON":
            reverse[edge["target"]].add(edge["source"])

    impact = defaultdict(set)
    for source in sorted(failed):
        visited = {source}
        queue = deque([(source, 0)])
        while queue:
            current, depth = queue.popleft()
            if depth >= 6:
                continue
            for dependent in sorted(reverse[current]):
                if dependent not in visited:
                    visited.add(dependent)
                    impact[dependent].add(source)
                    queue.append((dependent, depth + 1))
                elif dependent != source:
                    impact[dependent].add(source)
    for node in nodes:
        node["potentially_impacted_by"] = sorted(impact.get(node["id"], set()))

    counts = {value: sum(n["status"] == value for n in nodes)
              for value in ("ok", "warning", "error", "stale", "unknown")}
    return {
        "schema_version": 1,
        "kind": "LOCAL_CONTROL_ROOM_STATE",
        "mode": "SOURCE_REPORTED_NOT_INDEPENDENTLY_ATTESTED",
        "generated_at": now.isoformat().replace("+00:00", "Z"),
        "project": topology["project"],
        "nodes": nodes,
        "edges": graph_edges,
        "source_evidence": topology["source_evidence"],
        "unresolved": topology["unresolved"],
        "gaps": gaps,
        "coverage": {**topology["coverage"],
                     "node_status_counts": counts,
                     "observed_nodes": sum(n["observation_source"] is not None for n in nodes),
                     "unknown_or_stale_nodes": counts["unknown"] + counts["stale"]},
        "impact_semantics": "DECLARED_DEPENDENCIES_POTENTIAL_IMPACT_NOT_CAUSAL_PROOF",
        "leading_indicator_semantics": "USER_CONFIGURED_THRESHOLD_NOT_OUTAGE_PREDICTION",
        "runtime_discovery": "NOT_IMPLEMENTED",
        "execution_authorized": False,
    }


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>OntologyEX / Control Room</title>
<style>
:root{color-scheme:dark;font-family:ui-monospace,SFMono-Regular,Consolas,monospace;
background:#071018;color:#d5e9eb}*{box-sizing:border-box}body{margin:0;background:radial-gradient(ellipse at 50% -20%,#133a4b,#071018 58%);
min-height:100vh}header{padding:20px 24px;border-bottom:1px solid #254452;display:flex;align-items:center;
justify-content:space-between;gap:20px}h1{font-size:19px;letter-spacing:.12em;margin:0}small,.muted{color:#839fab}
header small{display:block;letter-spacing:.11em;margin-bottom:6px;color:#50b5af}
main{display:grid;grid-template-columns:minmax(0,1fr) 325px;gap:12px;padding:14px;min-height:calc(100vh - 90px)}
#map{min-height:590px;max-height:calc(100vh - 120px);overflow:auto;background:
linear-gradient(90deg,transparent 49.8%,#17324135 50%,transparent 50.2%),
repeating-linear-gradient(0deg,transparent 0,transparent 31px,#17324134 32px);
border:1px solid #284350;border-radius:12px}#svg{width:100%;display:block;min-height:570px}
aside{border:1px solid #284350;border-radius:12px;background:#0c1d28;display:flex;
flex-direction:column;gap:20px;padding:18px;overflow:auto;max-height:calc(100vh - 120px)}
h2{font-size:12px;letter-spacing:.13em;text-transform:uppercase;color:#4fbfbb;margin:0 0 12px}
p{font-size:12px;line-height:1.7;overflow-wrap:anywhere}#counts{display:flex;gap:8px;flex-wrap:wrap}
.chip{font-size:11px;border:1px solid #325361;border-radius:6px;padding:6px 10px}
button{border:1px solid #386372;border-radius:7px;background:#18323d;color:#cbf1f0;
cursor:pointer;padding:9px 14px;font:inherit}button:hover{background:#244956}
.status{font-weight:700;letter-spacing:.08em}.ok{color:#49c4a4}.warning{color:#eebc63}
.error{color:#f47f7a}.stale{color:#b79ff2}.unknown{color:#8196a5}
#details{white-space:pre-wrap;font-size:12px;overflow-wrap:anywhere;color:#c5d6d9;line-height:1.7}
#footer{padding:0 24px 14px;color:#78939f;font-size:11px}
@media(max-width:850px){main{grid-template-columns:1fr}aside{max-height:none}#map{max-height:70vh}}
</style></head>
<body><header><div><small>ONTOLOGYEX // LOCAL OBSERVABILITY</small>
<h1>BUSINESS CONTROL ROOM</h1></div><div><button id="map-business" type="button">Map My Business</button><div id="mode" class="muted">Connecting to local source...</div><small id="updated"></small></div></header>
<main><section id="map" aria-label="Declared component topology"><svg id="svg" role="img" aria-label="Interactive dependency diagram"></svg></section>
<aside><section><h2>System integrity</h2><div id="counts"></div></section>
<section><h2>Selected component</h2><div id="details">Select a node to inspect its source and status.</div></section>
<section><h2>Coverage and blind spots</h2><div id="coverage" class="muted"></div></section>
<section><h2>Operational boundary</h2>
<p class="muted">A declared dependency is not proof of a live network call. Source-reported status is not independently authenticated.
Warnings require explicit observations or configured thresholds. Impact shows possible dependents, not confirmed outages.
Unknown remains unknown. No repair actions run here.</p></section></aside></main>
<div id="footer">READ ONLY · LOOPBACK ONLY · NO LIVE PROVIDER CONNECTION · REFRESH 5S</div>
<script>
"use strict";
const NS="http://www.w3.org/2000/svg";
const color={ok:"#49c4a4",warning:"#eebc63",error:"#f47f7a",stale:"#b79ff2",unknown:"#69889c"};
const el=(tag,attrs={},txt)=>{const n=document.createElementNS(NS,tag);for(const [k,v] of Object.entries(attrs))n.setAttribute(k,String(v));
 if(txt!==undefined)n.textContent=String(txt);return n};
const text=(id,value)=>document.getElementById(id).textContent=String(value);
let selected=null,state=null;
function group(n){if(n.kind==="project")return 0;if(n.kind==="declared_service")return 2;if(n.kind==="workflow")return 1;return 1}
function draw(s){
 state=s;text("mode",s.mode);text("updated","As of "+s.generated_at);
 const count=s.coverage.node_status_counts;
 const counts=document.getElementById("counts");counts.replaceChildren();
 for(const k of ["ok","warning","error","stale","unknown"]){let b=document.createElement("span");b.className="chip "+k;
 b.textContent=k.toUpperCase()+" "+(count[k]||0);counts.append(b)}
 const cov=document.getElementById("coverage");cov.replaceChildren();
 const lines=["Candidate manifests: "+s.coverage.candidate_manifests,
 "Monitored nodes: "+s.coverage.observed_nodes+"/"+s.nodes.length,
 "Unknown / stale: "+s.coverage.unknown_or_stale_nodes,
 "Unresolved links: "+s.unresolved.length,"Discovery gaps: "+s.gaps.length,
 "Not assessed: "+s.coverage.not_assessed.join(", ")];
 for(const l of lines){const p=document.createElement("p");p.textContent=l;cov.append(p)}
 const svg=document.getElementById("svg");svg.replaceChildren();
 const cols=[[],[],[]];for(const n of s.nodes)cols[group(n)].push(n);
 for(const col of cols)col.sort((a,b)=>a.label.localeCompare(b.label));
 const rowH=76,height=Math.max(550,...cols.map(a=>a.length*rowH+135)),width=1060;
 svg.setAttribute("viewBox","0 0 "+width+" "+height);svg.style.height=height+"px";
 const starts=[50,392,740],nodeW=260,nodeH=48,pos={};
 for(let i=0;i<3;i++){svg.append(el("text",{x:starts[i],y:45,fill:"#679baa","font-size":12,"letter-spacing":3},
 ["BUSINESS","WORKFLOWS / SOURCES","SERVICES / DATA"][i]));
 cols[i].forEach((n,j)=>pos[n.id]={x:starts[i],y:75+j*rowH})}
 for(const e of s.edges){const a=pos[e.source],b=pos[e.target];if(!a||!b)continue;
 const x1=a.x+nodeW,y1=a.y+nodeH/2,x2=b.x,y2=b.y+nodeH/2;
 const d="M"+x1+","+y1+" C"+(x1+38)+","+y1+" "+(x2-38)+","+y2+" "+x2+","+y2;
 svg.append(el("path",{d,stroke:e.relation==="DEPENDS_ON"?"#e5b66f":"#2e8297",
 "stroke-opacity":.65,"stroke-width":e.relation==="DEPENDS_ON"?2:1.35,
 "stroke-dasharray":e.relation==="DEPENDS_ON"?"":"5 6",fill:"none"}))}
 for(const n of s.nodes){const p=pos[n.id],g=el("g",{role:"button",tabindex:0,
 "aria-label":n.label+" "+n.status,style:"cursor:pointer"});
 const active=selected===n.id,stroke=active?"#d6f6ef":color[n.status];
 g.append(el("rect",{x:p.x,y:p.y,width:nodeW,height:nodeH,rx:9,fill:"#102531",
 stroke,"stroke-width":active?2:1.25}));
 g.append(el("circle",{cx:p.x+19,cy:p.y+nodeH/2,r:6,fill:color[n.status]}));
 const label=n.label.length>29?n.label.slice(0,26)+"…":n.label;
 g.append(el("text",{x:p.x+36,y:p.y+20,fill:"#dce9e8","font-size":13,"font-weight":"600"},label));
 g.append(el("text",{x:p.x+36,y:p.y+35,fill:"#8ea9b3","font-size":10},
 n.status.toUpperCase()+" · "+n.kind.replaceAll("_"," ")));
 if(n.potentially_impacted_by.length)g.append(el("text",{x:p.x+nodeW-8,y:p.y+14,
 "text-anchor":"end",fill:"#eebc63","font-size":10},"RISK"));
 g.addEventListener("click",()=>pick(n.id));g.addEventListener("keydown",ev=>{if(ev.key==="Enter")pick(n.id)});
 svg.append(g)}
 const target=s.nodes.find(n=>n.id===selected);if(target)detail(target,s);
}
function pick(id){selected=id;if(state)draw(state)}
function detail(n,s){
 const ev=s.source_evidence.find(e=>e.id===n.evidence_id);
 const relevant=s.edges.filter(e=>e.source===n.id||e.target===n.id);
 const lines=[n.label.toUpperCase(),"", "STATE: "+n.status.toUpperCase(),
 "TYPE: "+n.kind,"", "LAST OBSERVATION: "+(n.observed_at||"none"),
 "SOURCE: "+(n.observation_source||"none"),"TRUST: "+n.source_authenticity,
 "REPORTED STATUS: "+(n.source_status||"unknown"),
 "METRICS: "+JSON.stringify(n.metrics), "LEADING SIGNALS: "+JSON.stringify(n.leading_indicators),
 "","DECLARED CONNECTIONS: "+relevant.length,
 ...relevant.slice(0,20).map(e=>e.relation+" : "+(e.source===n.id?e.target:e.source)),
 "","POTENTIALLY IMPACTED BY: "+(n.potentially_impacted_by.join(", ")||"none"),
 "","SOURCE FILE: "+(ev?.path||"not captured"),
 "EVIDENCE HASH: "+(ev?.sha256||"unknown"),
 "","No causal root cause is asserted."];
 text("details",lines.join("\n"));
}
async function refresh(){
 try{const res=await fetch("/api/state",{cache:"no-store"});if(!res.ok)throw Error("source not available");
 const s=await res.json();draw(s)}catch(err){text("mode","DATA UNAVAILABLE · LAST VIEW IS STALE");
 text("updated","Latest fetch failed");}
}
let mapStarted=false;
document.getElementById("map-business").addEventListener("click",()=>{
 mapStarted=true;refresh();
});
setInterval(()=>{if(mapStarted)refresh()},5000);
</script></body></html>"""


def serve(repo: Path | None, observations: Path | None, thresholds: Path | None,
          port: int, github: str | None = None, github_refresh_seconds: int = 300,
          github_client=None) -> None:
    check(0 <= port <= 65535, "invalid port")
    rules = read_thresholds(thresholds)
    check(60 <= github_refresh_seconds <= 3600, "invalid GitHub refresh interval")
    check((repo is None) != (github is None), "configure exactly one source")
    if github:
        github_discovery.parse_repository(github)
        client = github_client or github_discovery.GitHubClient(
            token=os.environ.get("ONTOLOGYEX_GITHUB_TOKEN"))
    else:
        client = None
    # Discovery is read-only and can change while serving. Limit update cadence.
    state_cache: dict = {}
    cache_lock = RLock()

    def snapshot() -> dict:
        current = datetime.now(timezone.utc)
        with cache_lock:
            key = state_cache.get("time")
            interval = github_refresh_seconds if github else 15
            if key is None or (current - key).total_seconds() > interval:
                try:
                    topology = (github_discovery.discover_github(github, client)
                                if github else discover_repo.discover(repo))
                except (github_discovery.GitHubDiscoveryError, OSError,
                        discover_repo.DiscoveryError):
                    if "topology" not in state_cache:
                        raise
                    state_cache["refresh_error"] = True
                else:
                    state_cache["topology"] = topology
                    state_cache["time"] = current
                    state_cache["refresh_error"] = False
            topology = state_cache["topology"]
            refresh_error = state_cache.get("refresh_error", False)
        result = build_state(topology, read_observations(observations), rules, current)
        result["discovery_source"] = topology.get("source", {"provider": "local"})
        result["discovery_freshness"] = (
            "STALE_LAST_KNOWN" if refresh_error else "AS_OF_LATEST_SUCCESSFUL_SCAN")
        if refresh_error:
            result["gaps"].append({"source": "github", "reason": "GITHUB_REFRESH_UNAVAILABLE"})
        return result

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            host_headers = self.headers.get_all("Host", [])
            if (len(host_headers) != 1 or not valid_local_host(
                    host_headers[0], self.headers.get("Origin"), self.server.server_port)):
                self.send_response(403)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if self.path == "/":
                payload, mime, code = PAGE.encode("utf-8"), "text/html; charset=utf-8", 200
            elif self.path == "/api/state":
                try:
                    value = snapshot()
                    payload, code = json.dumps(value, sort_keys=True).encode(), 200
                except (RoomError, discover_repo.DiscoveryError, github_discovery.GitHubDiscoveryError,
                        OSError, ValueError) as exc:
                    payload = json.dumps({"status": "ERROR", "reason": type(exc).__name__}).encode()
                    code = 503
                mime = "application/json; charset=utf-8"
            else:
                payload, mime, code = b"Not found", "text/plain", 404
            self.send_response(code)
            self.send_header("Content-Type", mime)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; connect-src 'self'; style-src 'unsafe-inline'; "
                             "script-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            return  # No user-supplied paths or request logs persisted.

    # No POST, no write operations and no external listener.
    with ThreadingHTTPServer(("127.0.0.1", port), Handler) as server:
        print(f"OntologyEX local control room: http://127.0.0.1:{server.server_port}",
              flush=True)
        server.serve_forever()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--repo", type=Path, help="existing local repository")
    source.add_argument("--github", help="GitHub owner/name; optional token via env")
    parser.add_argument("--github-refresh-seconds", type=int, default=300)
    parser.add_argument("--observations", type=Path, default=None)
    parser.add_argument("--thresholds", type=Path, default=None)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--state-only", action="store_true")
    args = parser.parse_args()
    try:
        if args.repo is not None:
            check(args.repo.is_dir() and not args.repo.is_symlink(),
                  "invalid repository root")
        if args.github is not None:
            github_discovery.parse_repository(args.github)
        if args.state_only:
            topology = (github_discovery.discover_github(
                args.github, github_discovery.GitHubClient(
                    token=os.environ.get("ONTOLOGYEX_GITHUB_TOKEN")))
                        if args.github else discover_repo.discover(args.repo))
            result = build_state(topology, read_observations(args.observations),
                                 read_thresholds(args.thresholds))
            result["discovery_source"] = topology.get("source", {"provider": "local"})
            print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        else:
            serve(args.repo, args.observations, args.thresholds, args.port,
                  github=args.github, github_refresh_seconds=args.github_refresh_seconds)
        return 0
    except (OSError, RoomError, discover_repo.DiscoveryError,
            github_discovery.GitHubDiscoveryError, ValueError, TypeError,
            RecursionError) as exc:
        print(json.dumps({"status": "ERROR", "reason": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
