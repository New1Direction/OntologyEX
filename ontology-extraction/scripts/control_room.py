#!/usr/bin/env python3
"""OntologyEX local control room: declared topology + independently supplied status.

Read-only, bound to loopback, no agent execution, no outgoing network calls.
The UI is a local prototype. Input samples are source-reported, not attested.
"""
from __future__ import annotations

import argparse
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Lock, RLock, Thread
import hmac
import json
import math
import os
import re
import secrets
import shlex
import stat

import business_view
import code_surface
import decide
import discover_repo
import evidence_grade
import decisions
import dependency_suggest
import change_impact
import entity_context
import watermark
import link_suggest
import rule_suggest
import suppliers
import command_center
import digest
import drift
import guardrail
import ownership
import compliance
import operations
import pulls
import background_scans
import stale_view
import github_discovery
import snapshot_store
import graph_view_api
import incident_view
import native_view_bridge
from policy_check import DomainError
from urllib.parse import parse_qs, urlsplit

MAX_OBSERVATION_BYTES = 1024 * 1024
MAX_OBSERVATIONS = 4096
MAX_THRESHOLDS_BYTES = 16384
METRICS = frozenset({"queue_lag_seconds", "db_pool_utilization_pct",
                     "error_rate_pct", "p95_latency_ms", "job_lag_seconds",
                     "probe_latency_ms"})
PERCENT_METRICS = frozenset({"db_pool_utilization_pct", "error_rate_pct"})
STATUS = frozenset({"ok", "warning", "error"})
SOURCES = frozenset({"otel", "health_probe", "prometheus", "manual_export", "synthetic"})
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


CODE_SURFACE_SECONDS = 60
OWNERSHIP_SECONDS = 300  # git history and CODEOWNERS change slowly; a long log is not read every minute


def with_code_surface(topology: dict, repo: Path, surface: dict | None = None) -> dict:
    """A local topology plus the routes and tables its code declares; a gap, not a failure, if unreadable."""
    try:
        return code_surface.attach(topology, repo, surface=surface)
    except (ValueError, OSError) as exc:
        return {**topology, "gaps": [*topology.get("gaps", []),
                                     {"source": "code", "reason": f"code not scanned ({type(exc).__name__})"}]}


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
        records.sort(key=lambda r: (r["observed_at"], r["source"], r["status"]))
        # Never let JSONL line order decide health: one source that disagrees
        # with itself at its newest timestamp fails closed. Different sources
        # are combined below by severity, which no line order can change.
        for source in {r["source"] for r in records}:
            mine = [r for r in records if r["source"] == source]
            newest = [r for r in mine if r["observed_at"] == mine[-1]["observed_at"]]
            if len({json.dumps(r, sort_keys=True) for r in newest}) > 1:
                conflicts.add(component_id)
        if component_id in conflicts:
            gaps.append({"source": component_id,
                         "reason": "CONFLICTING_SAME_TIMESTAMP_OBSERVATIONS"})

    severity = {"error": 0, "warning": 1, "ok": 2}
    nodes = []
    failed = set()
    for node in graph_nodes:
        values = grouped.get(node["id"], [])
        latest = values[-1] if values else None
        state = "unknown"
        leading = []
        sources = []
        if latest is not None and node["id"] not in conflicts:
            # Newest sample per source; each source expires on its own TTL.
            per_source = {}
            for record in values:
                per_source[record["source"]] = record
            fresh = []
            for source, record in sorted(per_source.items()):
                age = (now - timestamp(record["observed_at"])).total_seconds()
                is_fresh = age <= record["expires_after_seconds"]
                sources.append({"source": source, "status": record["status"],
                                "observed_at": record["observed_at"], "fresh": is_fresh})
                if is_fresh:
                    fresh.append(record)
            if not fresh:
                state = "stale"
            else:
                # The most severe fresh source decides; an ok elsewhere cannot hide it.
                worst = min(severity[r["status"]] for r in fresh)
                latest = max((r for r in fresh if severity[r["status"]] == worst),
                             key=lambda r: (r["observed_at"], r["source"]))
                state = latest["status"]
                for current in fresh:
                    mine = [r for r in values if r["source"] == current["source"]]
                    if len(mine) < 2:
                        continue
                    prior = mine[-2]
                    prior_age = (now - timestamp(prior["observed_at"])).total_seconds()
                    if prior_age > prior["expires_after_seconds"]:
                        continue
                    for metric, threshold in sorted(thresholds.items()):
                        if (metric in prior["metrics"] and metric in current["metrics"] and
                                prior["metrics"][metric] > threshold and
                                current["metrics"][metric] > threshold):
                            leading.append({
                                "metric": metric, "threshold": threshold,
                                "current": current["metrics"][metric],
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
            "source_line": node.get("source_line"),
            "observation_sources": sources,
            "potentially_impacted_by": [],
        })

    reverse = defaultdict(set)
    for edge in graph_edges:
        check(edge["source"] in by_id and edge["target"] in by_id,
              "topology contains dangling edge")
        # Directed from consumer to dependency: declared (DEPENDS_ON), or seen in
        # traces and confirmed by a person (CALLS, from dependency_suggest).
        if edge["relation"] in ("DEPENDS_ON", "CALLS"):
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
    ci_by_node = {
        record["workflow_node_id"]: record
        for record in topology.get("ci_evidence", [])
        if type(record) is dict and type(record.get("workflow_node_id")) is str
           and record.get("scope") == "CI_ONLY_NOT_PRODUCTION_HEALTH"
    }
    for node in nodes:
        node["potentially_impacted_by"] = sorted(impact.get(node["id"], set()))
        node["ci_run"] = (ci_by_node.get(node["id"])
                          if node["kind"] == "workflow" else None)

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
        "ci_evidence_semantics": "GITHUB_ACTIONS_RUN_AT_PINNED_COMMIT_NOT_PRODUCTION_HEALTH",
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
#business-view,#entity-view{display:inline-block;margin-top:6px;font-size:12px;color:#7fd6d2}
#entity-page{display:block;margin-top:8px;font-size:12px;color:#7fd6d2}
main#workspace{position:relative;display:flex;flex-direction:column;gap:12px;padding:12px;
width:100%;min-height:520px;overflow:visible}
#workspace>section{position:relative;min-width:0;min-height:120px;box-sizing:border-box;overflow:auto;
scrollbar-width:thin;transition:none}
#workspace[data-layout-ready="true"]>section{transition:top .22s ease,left .22s ease,width .22s ease,height .22s ease}
#map{min-height:320px;overflow:auto;background:
linear-gradient(90deg,transparent 49.8%,#17324135 50%,transparent 50.2%),
repeating-linear-gradient(0deg,transparent 0,transparent 31px,#17324134 32px);
border:1px solid #284350;border-radius:12px}#svg{width:100%;display:block;min-height:570px}
#workspace>section:not(#map){border:1px solid #284350;border-radius:12px;background:#0c1d28;padding:14px;overflow:auto}
#workspace>section.priority-critical{border-color:#52747f}
h2{font-size:12px;letter-spacing:.13em;text-transform:uppercase;color:#4fbfbb;margin:0 0 12px}
p{font-size:12px;line-height:1.7;overflow-wrap:anywhere}#counts{display:flex;gap:8px;flex-wrap:wrap}
.chip{font-size:11px;border:1px solid #325361;border-radius:6px;padding:6px 10px}
button{border:1px solid #386372;border-radius:7px;background:#18323d;color:#cbf1f0;
cursor:pointer;padding:9px 14px;font:inherit}button:hover{background:#244956}
.status{font-weight:700;letter-spacing:.08em}.ok{color:#49c4a4}.warning{color:#eebc63}
.error{color:#f47f7a}.stale{color:#b79ff2}.unknown{color:#8196a5}
#details{white-space:pre-wrap;font-size:12px;overflow-wrap:anywhere;color:#c5d6d9;line-height:1.7}
#footer{padding:0 24px 14px;color:#78939f;font-size:11px}
header{flex-wrap:wrap}header>div:last-child{display:flex;flex-wrap:wrap;align-items:center;gap:8px}
header>div:last-child #mode{flex-basis:100%;font-size:11px}
#graph-search{width:min(180px,42vw);border-radius:6px;background:#0f2530;border:1px solid #386372;color:#d5e9eb;padding:8px}
#lens{border-radius:6px;background:#0f2530;color:#d5e9eb;border:1px solid #386372;padding:7px}
#graph-coverage{font-size:11px;color:#a9c7ce;margin:0;padding:6px}
#graph-legend{font-size:10px;color:#8ea9b3;padding:1px 8px}
#graph-pager{display:flex;gap:8px;padding:2px 8px}
#graph-pager button:disabled{opacity:.42;cursor:default}
#graph-scope{font-size:11px;color:#8fa9b5;min-height:17px;padding:0 8px}
#graph-depth{border-radius:6px;background:#0f2530;color:#d5e9eb;border:1px solid #386372;padding:7px}
#graph-pager button:focus-visible,#graph-depth:focus-visible{outline:2px solid #50b5af}
#graph-share{accent-color:#50b5af;max-width:115px}
#versions-panel[data-collapsed="true"] #versions{display:none}
#incidents ul{padding-left:19px;margin:0;font-size:12px;line-height:1.6}
#layout-debug{margin:0 14px 16px;padding:8px 12px;font-size:11px;border:1px solid #345264;border-radius:7px;max-width:800px}
#layout-report{white-space:pre-wrap;overflow-wrap:anywhere;max-height:180px;overflow:auto}
#incident-report{white-space:pre-wrap;overflow-wrap:anywhere;font-size:11px;line-height:1.6;max-height:320px;overflow:auto;color:#9dbdc2}
@media(prefers-reduced-motion:reduce){#workspace>section{transition:none!important}}
</style></head>
<body><header><div><small>ONTOLOGYEX // LOCAL OBSERVABILITY</small>
<h1>BUSINESS CONTROL ROOM</h1><a id="business-view" href="/business">Business operating picture →</a> <a id="entity-view" href="/entity">Entities →</a></div><div><select id="lens" aria-label="Graph lens">
<option value="all">ALL SYSTEMS</option><option value="api">API ROUTES</option>
<option value="database">DATABASE</option><option value="services">SERVICES</option>
</select> <input id="graph-search" type="search" aria-label="Find graph node" maxlength="90" placeholder="Find node…"/>
<label for="graph-share" title="Resize the graph area">Graph size</label>
<input id="graph-share" type="range" min="48" max="76" value="63" aria-label="Graph area width"/>
<button id="graph-focus" type="button" aria-pressed="false">Focus graph</button> <button id="map-business" type="button">Map My Business</button><div id="mode" class="muted">Connecting to local source...</div><small id="updated"></small></div></header>
<main id="workspace" aria-label="Adaptive business workspace">
<section id="map" aria-label="Declared component topology"><p id="graph-coverage" aria-live="polite"></p><div id="graph-legend" aria-label="Graph edge legend">
<span>Amber arrows: declared dependency. Violet arrows: dependency seen in traces and confirmed by a person. Teal dashed arrows: other source relationships. Direction is source → target, not runtime causation.</span></div>
<div id="graph-pager">
<button id="graph-back" type="button" disabled>Back to list</button>
<label for="graph-depth">Hops</label>
<select id="graph-depth" aria-label="Neighborhood depth"><option value="1">1 hop</option><option value="2">2 hops</option></select>
<button id="graph-prev" type="button" disabled>Previous</button>
<button id="graph-next" type="button" disabled>Next</button>
</div><p id="graph-scope" aria-live="polite">Select a node to explore its connected neighbors.</p><svg id="svg" role="img" aria-label="Interactive dependency diagram"></svg></section>
<section id="incidents" class="priority-critical"><h2>Incidents and risks</h2><ul id="incident-list"></ul></section>
<section id="integrity" class="priority-critical"><h2>System integrity</h2><div id="counts"></div></section>
<section id="details-panel"><h2>Selected component</h2><div id="details">Select a node to inspect its source and status.</div><button type="button" id="investigate-node" disabled>Investigate evidence</button><a id="entity-page" href="/entity" hidden>Entity page: why it exists, what relies on it →</a><pre id="incident-report" aria-live="polite">Select a node to inspect its observations and possible exposures.</pre></section>
<section id="coverage-panel"><h2>Coverage and blind spots</h2><div id="coverage" class="muted"></div></section>
<section id="versions-panel" data-collapsed="false"><h2>Saved source revisions</h2>
<button id="toggle-versions" type="button" aria-expanded="true" aria-controls="versions">Collapse revisions</button>
<div id="versions" class="muted"></div></section>
<section id="boundary-panel"><h2>Operational boundary</h2>
<p class="muted">A declared dependency is not proof of a live network call. Source-reported status is not independently authenticated.
Warnings require explicit observations or configured thresholds. Impact shows possible dependents, not confirmed outages.
Unknown remains unknown. No repair actions run here.</p></section></main>
<details id="layout-debug"><summary>Layout diagnostics</summary><pre id="layout-report">Layout not yet calculated.</pre></details>
<div id="footer">READ ONLY · LOOPBACK ONLY · NO LIVE PROVIDER CONNECTION · REFRESH 5S</div>
<script src="/static/layout_engine.js"></script>
<script>
"use strict";
const NS="http://www.w3.org/2000/svg";
const color={ok:"#49c4a4",warning:"#eebc63",error:"#f47f7a",stale:"#b79ff2",unknown:"#69889c"};
const el=(tag,attrs={},txt)=>{const n=document.createElementNS(NS,tag);for(const [k,v] of Object.entries(attrs))n.setAttribute(k,String(v));
 if(txt!==undefined)n.textContent=String(txt);return n};
const text=(id,value)=>document.getElementById(id).textContent=String(value);
let selected=null,state=null,focusGraph=false,lastLayout=null,graphShare=0.63,versionsCollapsed=false;
let graphOffset=0,requestEpoch=0,lastSummary=null,lastPage=null,neighborhoodRoot=null;
let detailEpoch=0,incidentEpoch=0;
try{
 focusGraph=localStorage.getItem("ontologyex-focus-graph")==="1";
 const saved=Number(localStorage.getItem("ontologyex-graph-share"));
 if(Number.isFinite(saved)&&saved>=0.48&&saved<=0.76)graphShare=saved;
 versionsCollapsed=localStorage.getItem("ontologyex-versions-collapsed")==="1";
}catch(_){}
document.getElementById("graph-focus").setAttribute("aria-pressed",String(focusGraph));
document.getElementById("graph-share").value=String(Math.round(graphShare*100));
document.getElementById("versions-panel").dataset.collapsed=String(versionsCollapsed);
document.getElementById("toggle-versions").setAttribute("aria-expanded",String(!versionsCollapsed));
document.getElementById("toggle-versions").textContent=versionsCollapsed?"Expand revisions":"Collapse revisions";
const PANEL_SPECS=[
 {id:"map",minWidth:240,minHeight:310,preferredHeight:540,priority:78},
 {id:"incidents",minWidth:200,minHeight:116,preferredHeight:145,priority:98,critical:true},
 {id:"integrity",minWidth:200,minHeight:82,preferredHeight:92,priority:96,critical:true},
 {id:"details-panel",minWidth:200,minHeight:180,preferredHeight:235,priority:85},
 {id:"coverage-panel",minWidth:200,minHeight:142,preferredHeight:180,priority:62},
 {id:"versions-panel",minWidth:200,minHeight:88,preferredHeight:108,priority:35},
 {id:"boundary-panel",minWidth:200,minHeight:104,preferredHeight:124,priority:32}
];
function renderIncidents(s){
 const ul=document.getElementById("incident-list");ul.replaceChildren();
 const serious=Array.isArray(s.alerts)?s.alerts:[];
 if(!serious.length){
   const li=document.createElement("li");
   li.textContent="No current reported alerts. Unobserved components are NOT confirmed healthy.";
   ul.append(li);
 }
 for(const n of serious.slice(0,18)){
   const li=document.createElement("li"),btn=document.createElement("button");
   btn.textContent=(n.status.toUpperCase()==="UNKNOWN"?"POSSIBLE IMPACT":n.status.toUpperCase())+" · "+n.label;
   btn.addEventListener("click",()=>{pick(n.id)});
   li.append(btn);ul.append(li);
 }
 if((s.total_alerts||0)>serious.length){
   const li=document.createElement("li");
   li.textContent=(s.total_alerts-serious.length)+" more alerts. Search the graph to inspect.";
   ul.append(li);
 }
}
function applyLayout(s){
 const root=document.getElementById("workspace");
 const width=Math.max(280,root.clientWidth),header=document.querySelector("header");
 const height=Math.max(260,window.innerHeight-header.getBoundingClientRect().height-60);
 const urgent=s.total_alerts||0;
 const dims=PANEL_SPECS.map(p=>({...p,collapsed:p.id==="versions-panel"&&versionsCollapsed,
   items:p.id==="incidents"?urgent:
   p.id==="details-panel"?(selected?22:3):
   p.id==="coverage-panel"?((s.total_gaps||0)+(s.unresolved_count||0)):
   p.id==="versions-panel"?(s.snapshot_history?.length||0):0}));
 try{
   const plan=OntologyLayout.optimize({width,height,panels:dims,density:s.total_nodes||s.nodes.length,focusGraph,
     mapShare:graphShare});
   root.style.display="block";root.style.padding="0";root.style.gap="0";
   root.style.height=plan.contentHeight+"px";
   for(const [id,r] of Object.entries(plan.rects)){
     const el=document.getElementById(id);
     el.style.position="absolute";el.style.minHeight="0px";
     el.style.left=r.x+"px";el.style.top=r.y+"px";
     el.style.width=r.width+"px";el.style.height=r.height+"px";
   }
   lastLayout=plan;
   // Geometry is verified after the 220ms CSS transition settles.
   // Ignore an obsolete check if viewport/content changed meanwhile.
   setTimeout(()=>{
     if(lastLayout!==plan)return;
     // Animate only after the first stable layout; never animate from the
     // safe pre-discovery stacked state into absolute coordinates.
     root.dataset.layoutReady="true";
     const result=OntologyLayout.verifyDOM(plan,root);
     const report={columns:plan.columns,score:plan.score,contentHeight:plan.contentHeight,
       candidates:plan.candidates,decisions:plan.decisions,
       geometryPassed:plan.constraints.ok,renderedDOMPassed:result.ok,
       violations:result.violations,rects:plan.rects};
     document.getElementById("layout-report").textContent=JSON.stringify(report,null,2);
   },280);
 }catch(e){
   lastLayout=null;
   root.style.cssText="display:flex;flex-direction:column;gap:12px;height:auto;padding:12px";
   for(const p of dims){
     const el=document.getElementById(p.id);
     el.style.cssText="position:relative;left:auto;top:auto;width:100%;height:"+Math.max(p.minHeight,260)+"px";
   }
   document.getElementById("layout-report").textContent="Safe stacked fallback: "+e.message;
 }
}
function group(n){if(n.kind==="project")return 0;
 if(n.kind==="declared_service"||n.kind==="declared_db_table"||n.kind==="source_module")return 2;
 return 1}
function draw(s){
 state=s;text("mode",s.mode+" / "+(s.discovery_freshness||"SOURCE_UNKNOWN"));
 text("updated","As of "+s.generated_at);
 const count=s.coverage.node_status_counts;
 const counts=document.getElementById("counts");counts.replaceChildren();
 for(const k of ["ok","warning","error","stale","unknown"]){let b=document.createElement("span");b.className="chip "+k;
 b.textContent=k.toUpperCase()+" "+(count[k]||0);counts.append(b)}
 const cov=document.getElementById("coverage");cov.replaceChildren();
 const lines=["Candidate manifests: "+s.coverage.candidate_manifests,
 "Monitored nodes: "+s.coverage.observed_nodes+"/"+s.total_nodes,
 "Unknown / stale: "+s.coverage.unknown_or_stale_nodes,
 "Unresolved links: "+s.unresolved_count,"Discovery gaps: "+s.total_gaps,
 "API route paths: "+(s.coverage.api_route_paths_found??"not scanned"),
 "DB table candidates: "+(s.coverage.db_schema_candidates_found??"not scanned"),
 "Verified static import links: "+(s.coverage.import_relationships_found??"not scanned"),
 "Routes checked for imports: "+(s.coverage.import_route_files_inspected??0)+"/"+
 (s.coverage.api_route_paths_found??0),
 "Not assessed: "+s.coverage.not_assessed.join(", ")];
 for(const l of lines){const p=document.createElement("p");p.textContent=l;cov.append(p)}
 renderIncidents(s);
 const versions=document.getElementById("versions");versions.replaceChildren();
 const saved=Array.isArray(s.snapshot_history)?s.snapshot_history:[];
 if(!saved.length){const p=document.createElement("p");p.textContent="No server-side revisions stored.";versions.append(p)}
 for(const item of saved.slice(0,8)){
   const p=document.createElement("p");
   p.textContent=(item.is_current?"CURRENT · ":"")+
     String(item.commit_sha||"unknown").slice(0,12)+" · "+String(item.scanned_at||"unknown");
   versions.append(p)
 }
 applyLayout(s);
 const svg=document.getElementById("svg");svg.replaceChildren();
 const visible=s.nodes.slice(0,90);
 const neighborhoodMode=s.kind==="BOUNDED_GRAPH_NEIGHBORHOOD";
 const matchesCount=s.total_matches??visible.length;
 document.getElementById("graph-back").disabled=!neighborhoodMode;
 document.getElementById("graph-prev").hidden=neighborhoodMode;
 document.getElementById("graph-next").hidden=neighborhoodMode;
 text("graph-scope",neighborhoodMode?
    "Connected source graph · "+s.requested_depth+" hop(s) · "+s.direct_incoming_edges+
    " incoming / "+s.direct_outgoing_edges+" outgoing at selected node · "+
    (s.is_complete_within_depth?"complete within modeled depth":"partial/bounded")+
    " · static declarations are NOT runtime execution":
    "Paged source inventory · click a node to explore incoming and outgoing relationships.");
 text("graph-coverage",neighborhoodMode?
    "Neighborhood: "+visible.length+" nodes, "+s.edges.length+" edges, "+
    s.truncated_node_candidates+" neighbor candidates excluded, "+
    s.edges_omitted+" internal edges omitted. No causal or runtime claims.":
    "Showing "+visible.length+" of "+matchesCount+" matching nodes ("+
    s.total_nodes+" total), offset "+(s.offset||0)+". "+
    (s.edges_omitted?"Edges omitted: "+s.edges_omitted+". ":"")+
    "Search and page through source-backed results.");
 document.getElementById("graph-prev").disabled=!(s.offset>0);
 document.getElementById("graph-next").disabled=s.next_offset==null;
 const severity={error:0,warning:1,stale:2,unknown:3,ok:4};
 const cols=[[],[],[]];for(const n of visible)cols[group(n)].push(n);
 for(const col of cols)col.sort((a,b)=>(a.id===selected?-1:severity[a.status]??5)-
   (b.id===selected?-1:severity[b.status]??5)||a.label.localeCompare(b.label));
 const rowH=64,height=Math.max(350,...cols.map(a=>a.length*rowH+88));
 const width=Math.max(780,document.getElementById("map").clientWidth-24);
 svg.setAttribute("viewBox","0 0 "+width+" "+height);
 svg.style.width=width+"px";svg.style.height=height+"px";
 const starts=[18,18+width/3,18+2*width/3],nodeW=Math.floor(width/3-35),nodeH=44,pos={};
 for(let i=0;i<3;i++){svg.append(el("text",{x:starts[i],y:45,fill:"#679baa","font-size":12,"letter-spacing":3},
 ["BUSINESS","WORKFLOWS / SOURCES","SERVICES / DATA"][i]));
 cols[i].forEach((n,j)=>pos[n.id]={x:starts[i],y:75+j*rowH})}
 // Arrowheads show the stored source -> target direction, even though
 // neighborhood navigation can traverse an edge from either endpoint.
 const defs=el("defs");
 for(const [id,fill] of [["dependency-arrow","#e5b66f"],["calls-arrow","#b59cff"],["source-arrow","#2e8297"]]){
   const marker=el("marker",{id,viewBox:"0 0 10 10",refX:9,refY:5,
     markerWidth:6,markerHeight:6,orient:"auto",markerUnits:"strokeWidth"});
   marker.append(el("path",{d:"M 0 0 L 10 5 L 0 10 z",fill}));
   defs.append(marker);
 }
 svg.append(defs);
 for(const e of s.edges){const a=pos[e.source],b=pos[e.target];if(!a||!b)continue;
 const x1=a.x+nodeW,y1=a.y+nodeH/2,x2=b.x,y2=b.y+nodeH/2;
 const d="M"+x1+","+y1+" C"+(x1+38)+","+y1+" "+(x2-38)+","+y2+" "+x2+","+y2;
 const dep=e.relation==="DEPENDS_ON",calls=e.relation==="CALLS";
 const path=el("path",{d,stroke:dep?"#e5b66f":calls?"#b59cff":"#2e8297",
 "stroke-opacity":.65,"stroke-width":dep||calls?2:1.35,
 "stroke-dasharray":dep||calls?"":"5 6",
 "marker-end":dep?"url(#dependency-arrow)":calls?"url(#calls-arrow)":"url(#source-arrow)",
 fill:"none"});
 path.append(el("title",{},e.relation+" · "+e.source+" → "+e.target+
   (e.source_line?" · source line "+e.source_line:"")+
   (calls?" · seen in traces and confirmed by a person; calls are source-reported":
    " · source relationship, not observed runtime execution")));
 svg.append(path)}
 for(const n of visible){const p=pos[n.id],g=el("g",{role:"button",tabindex:0,
 "aria-label":n.label+" "+n.status,style:"cursor:pointer"});
 const active=selected===n.id,stroke=active?"#d6f6ef":color[n.status];
 g.append(el("rect",{x:p.x,y:p.y,width:nodeW,height:nodeH,rx:9,fill:"#102531",
 stroke,"stroke-width":active?2:1.25}));
 g.append(el("circle",{cx:p.x+19,cy:p.y+nodeH/2,r:6,fill:color[n.status]}));
 const maxLabel=Math.max(16,Math.floor((nodeW-55)/7));
 const label=n.label.length>maxLabel?n.label.slice(0,maxLabel-1)+"…":n.label;
 g.append(el("text",{x:p.x+36,y:p.y+20,fill:"#dce9e8","font-size":13,"font-weight":"600"},label));
 g.append(el("text",{x:p.x+36,y:p.y+35,fill:"#8ea9b3","font-size":10},
 n.status.toUpperCase()+" · "+n.kind.replaceAll("_"," ")));
 if(n.potentially_impacted_by.length)g.append(el("text",{x:p.x+nodeW-8,y:p.y+14,
 "text-anchor":"end",fill:"#eebc63","font-size":10},"RISK"));
 if(neighborhoodMode&&n.id===s.center_id)g.append(el("text",{
  x:p.x+nodeW-8,y:p.y+14,"text-anchor":"end",fill:"#50d7cc","font-size":10
 },"CENTER"));
 if(n.ci_run)g.append(el("text",{x:p.x+nodeW-8,y:p.y+42,
 "text-anchor":"end",fill:n.ci_run.conclusion==="success"?"#49c4a4":"#eebc63",
 "font-size":10},"CI "+(n.ci_run.conclusion||n.ci_run.status).toUpperCase()));
 g.addEventListener("click",()=>pick(n.id));g.addEventListener("keydown",ev=>{if(ev.key==="Enter"||ev.key===" "){ev.preventDefault();pick(n.id)}});
 svg.append(g)}
 // Node detail is fetched separately, never inferred from a partial page.
}
async function loadNodeDetail(id){
 const seq=++detailEpoch;
 try{
   const res=await fetch("/api/node?id="+encodeURIComponent(id),{cache:"no-store"});
   if(!res.ok)throw Error("detail unavailable");
   const data=await res.json();
   if(seq!==detailEpoch||selected!==id)return;
   detail(data.node,data);
 }catch(_){
   if(seq===detailEpoch&&selected===id)
     text("details","Selected node detail unavailable. Current health cannot be inferred.");
 }
}
async function loadNeighborhood(id){
 const seq=++requestEpoch;
 const hops=document.getElementById("graph-depth").value;
 try{
   const response=await fetch("/api/neighborhood?id="+encodeURIComponent(id)+
     "&depth="+encodeURIComponent(hops),{cache:"no-store"});
   if(!response.ok)throw Error("neighborhood unavailable");
   const graph=await response.json();
   if(seq!==requestEpoch||neighborhoodRoot!==id)return;
   if(graph.kind!=="BOUNDED_GRAPH_NEIGHBORHOOD"||
      graph.summary?.kind!=="LOCAL_CONTROL_ROOM_SUMMARY"||
      graph.center_id!==id)throw Error("invalid neighborhood response");
   lastSummary=graph.summary;
   draw({...graph.summary,...graph});
 }catch(_){
   if(seq!==requestEpoch||neighborhoodRoot!==id)return;
   text("mode","NEIGHBORHOOD UNAVAILABLE · LAST VIEW MAY BE STALE");
   text("graph-scope","Could not verify connected source neighbors. Return to list or retry.");
 }
}
function pick(id){
 selected=id;neighborhoodRoot=id;
 ++incidentEpoch;
 document.getElementById("investigate-node").disabled=false;
 text("incident-report","Select Investigate evidence to load this node’s source-reported timeline.");
 if(state)draw(state);
 loadNodeDetail(id);
 return loadNeighborhood(id);
}
function detail(n,s){
 const ev=s.source_evidence.find(e=>e.id===n.evidence_id);
 const relevant=s.edges.filter(e=>e.source===n.id||e.target===n.id);
 const lines=[n.label.toUpperCase(),"", "STATE: "+n.status.toUpperCase(),
 "TYPE: "+n.kind,"", "LAST OBSERVATION: "+(n.observed_at||"none"),
 "SOURCE: "+(n.observation_source||"none"),"TRUST: "+n.source_authenticity,
 "REPORTED STATUS: "+(n.source_status||"unknown"),
 "METRICS: "+JSON.stringify(n.metrics), "LEADING SIGNALS: "+JSON.stringify(n.leading_indicators),
 "","DECLARED CONNECTIONS: "+s.total_related_edges+
 (s.edges_omitted?" ("+s.edges_omitted+" omitted)":""),
 "SHOWN CONNECTIONS: "+relevant.length,
 ...relevant.slice(0,20).map(e=>e.relation+" : "+
   (e.source===n.id?e.target:e.source)+
   (e.source_line?" [line "+e.source_line+"]":"")),
 "","POTENTIALLY IMPACTED BY: "+(n.potentially_impacted_by.join(", ")||"none"),
 "","GITHUB CI (NOT PRODUCTION HEALTH): "+(n.ci_run
   ? (n.ci_run.status+" / "+(n.ci_run.conclusion||"no result")) : "unavailable"),
 "CI RUN ID: "+(n.ci_run?.run_id||"none"),
 "CI UPDATED: "+(n.ci_run?.updated_at||"unknown"),
 "","SOURCE FILE: "+(ev?.path||"not captured"),
 "EVIDENCE HASH: "+(ev?.sha256||"unknown"),
 "SOURCE LINE: "+(n.source_line??"unknown"),
 "EVIDENCE TYPE: "+(ev?.source_kind||"unknown"),
 "","Database table names are source-syntax candidates until verified.",
 "No causal root cause is asserted."];
 text("details",lines.join("\n"));
 const entityPage=document.getElementById("entity-page");
 entityPage.href="/entity?id="+encodeURIComponent(n.id);entityPage.hidden=false;
}
async function loadPage(){
 const seq=++requestEpoch;
 try{
   const lens=document.getElementById("lens").value;
   const q=document.getElementById("graph-search").value.slice(0,90);
   const url="/api/view?lens="+encodeURIComponent(lens)+
     "&q="+encodeURIComponent(q)+"&offset="+graphOffset;
   const response=await fetch(url,{cache:"no-store"});
   if(!response.ok)throw Error("source unavailable");
   const pageData=await response.json();
   if(seq!==requestEpoch)return;
   const summaryData=pageData.summary;
   if(pageData.kind!=="BOUNDED_GRAPH_PAGE" || !summaryData ||
      summaryData.kind!=="LOCAL_CONTROL_ROOM_SUMMARY")throw Error("invalid data");
   lastSummary=summaryData;lastPage=pageData;
   draw({...summaryData,...pageData});
   if(selected)loadNodeDetail(selected);
 }catch(_){
   if(seq!==requestEpoch)return;
   text("mode","DATA UNAVAILABLE · LAST VIEW IS STALE");
   text("updated","Latest fetch failed; prior view may be stale");
 }
}
async function investigateSelected(){
 if(!selected)return;
 const id=selected,seq=++incidentEpoch;
 text("incident-report","Loading bounded source evidence…");
 try{
   const res=await fetch("/api/incident?id="+encodeURIComponent(id),{cache:"no-store"});
   if(!res.ok)throw Error("incident evidence unavailable");
   const record=await res.json();
   if(seq!==incidentEpoch||selected!==id||record.component_id!==id)return;
   const lines=[
     "INCIDENT EVIDENCE · SOURCE REPORTED ONLY",
     "COMPONENT: "+record.component.label,
     "STATUS: "+record.component.reported_state.toUpperCase(),
     "SOURCE TRUST: "+record.component.source_authenticity,
     "OBSERVATIONS: "+record.observations_included+"/"+record.observation_count,
     ...record.timeline.map(o=>o.observed_at+" "+o.status.toUpperCase()+
       " source="+o.source+" age="+o.age_seconds+"s "+
       (o.fresh_at_evaluation?"fresh":"stale")+" metrics="+JSON.stringify(o.metrics)),
     "POSSIBLY EXPOSED DEPENDENTS: "+record.possible_dependents.length+
       (record.possible_dependents_omitted?" (others omitted)":""),
     ...record.possible_dependents.map(n=>n.label+" · independently reported: "+n.independently_reported_status),
     "SOURCE REVISIONS: "+record.source_history.length+" (NOT deployments)",
     "ROOT CAUSE: UNDETERMINED",
     "VERIFY: "+record.recommended_diagnosis,
     "NO AUTHORIZATION TO REPAIR"
   ];
   text("incident-report",lines.join("\n"));
 }catch(_){
   if(seq===incidentEpoch&&selected===id)text("incident-report",
       "Incident evidence unavailable. Last viewed status may be stale.");
 }
}
document.getElementById("investigate-node").addEventListener("click",investigateSelected);
function refresh(){return neighborhoodRoot?loadNeighborhood(neighborhoodRoot):loadPage()}
let mapStarted=false;
function returnToList(){
 neighborhoodRoot=null;graphOffset=0;
 if(mapStarted)return loadPage();
}
document.getElementById("graph-back").addEventListener("click",returnToList);
document.getElementById("graph-depth").addEventListener("change",()=>{
 if(mapStarted&&neighborhoodRoot)loadNeighborhood(neighborhoodRoot);
});
document.getElementById("lens").addEventListener("change",()=>{graphOffset=0;neighborhoodRoot=null;if(mapStarted)loadPage()});
document.getElementById("graph-search").addEventListener("input",()=>{graphOffset=0;neighborhoodRoot=null;if(mapStarted)loadPage()});
document.getElementById("graph-prev").addEventListener("click",()=>{
 graphOffset=Math.max(0,graphOffset-90);loadPage();
});
document.getElementById("graph-next").addEventListener("click",()=>{
 if(lastPage?.next_offset!=null){graphOffset=lastPage.next_offset;loadPage()}
});
document.getElementById("graph-share").addEventListener("input",()=>{
 const value=Number(document.getElementById("graph-share").value)/100;
 if(!Number.isFinite(value)||value<0.48||value>0.76)return;
 graphShare=value;focusGraph=false;
 document.getElementById("graph-focus").setAttribute("aria-pressed","false");
 try{
   localStorage.setItem("ontologyex-graph-share",String(graphShare));
   localStorage.setItem("ontologyex-focus-graph","0");
 }catch(_){}
 if(state)draw(state);
});
document.getElementById("toggle-versions").addEventListener("click",()=>{
 versionsCollapsed=!versionsCollapsed;
 const panel=document.getElementById("versions-panel");
 panel.dataset.collapsed=String(versionsCollapsed);
 document.getElementById("toggle-versions").setAttribute("aria-expanded",String(!versionsCollapsed));
 document.getElementById("toggle-versions").textContent=versionsCollapsed?"Expand revisions":"Collapse revisions";
 try{localStorage.setItem("ontologyex-versions-collapsed",versionsCollapsed?"1":"0")}catch(_){}
 if(state)draw(state);
});
document.getElementById("graph-focus").addEventListener("click",()=>{
 focusGraph=!focusGraph;
 document.getElementById("graph-focus").setAttribute("aria-pressed",String(focusGraph));
 try{localStorage.setItem("ontologyex-focus-graph",focusGraph?"1":"0")}catch(_){}
 if(state)draw(state);
});
let resizeScheduled=false;
window.addEventListener("resize",()=>{
 if(resizeScheduled)return;
 resizeScheduled=true;
 requestAnimationFrame(()=>{resizeScheduled=false;if(state)draw(state)});
});
document.getElementById("map-business").addEventListener("click",()=>{
 mapStarted=true;refresh();
});
setInterval(()=>{if(mapStarted)refresh()},5000);
</script></body></html>"""


class RemoteInventory:
    """Atomically hold the last successful source-pinned inventory.

    Polling is driven by the server worker, never by a browser request.
    A refresh failure is visible; it never silently changes last-known bytes.
    """
    def __init__(self, scan, store=None, repository=None):
        self._scan = scan
        self._store = store
        self._lock = RLock()
        self._latest = None
        self._last_success = None
        self._failed = False
        if store is not None:
            check(type(repository) is str and repository, "snapshot source required")
            restored = store.load(repository)
            if restored is not None:
                self._latest, self._last_success = restored
                # Old durable data is NOT live. It stays stale until a fresh read.
                self._failed = True

    def refresh(self):
        try:
            value = self._scan()
        except (github_discovery.GitHubDiscoveryError,
                discover_repo.DiscoveryError, OSError, ValueError, TypeError,
                KeyError, RecursionError):
            with self._lock:
                self._failed = True
            return False
        moment = datetime.now(timezone.utc)
        try:
            if self._store is not None:
                self._store.save(value, moment)
        except (snapshot_store.SnapshotError, OSError, ValueError, TypeError):
            with self._lock:
                self._failed = True
            return False
        with self._lock:
            self._latest = value
            self._last_success = moment
            self._failed = False
        return True

    def snapshot(self):
        with self._lock:
            if self._latest is None:
                raise RoomError("source inventory not ready")
            return self._latest, self._last_success, self._failed


def serve(repo: Path | None, observations: Path | None, thresholds: Path | None,
          port: int, github: str | None = None, github_refresh_seconds: int = 300,
          github_client=None, snapshot_dir: Path | None = None,
          view_engine: str = "python", skills: list[Path] | None = None,
          links: Path | None = None, events: Path | None = None,
          allow_link_decisions: bool = False, decision_model: bool = False,
          discovery_engine: str = "python", dependencies: Path | None = None,
          print_bundle: bool = False, proposals: Path | None = None, rules_file: Path | None = None,
          history: Path | None = None, history_seconds: int = 300,
          operations_dir: Path | None = None, live_pulls: bool = False) -> None:
    check(0 <= port <= 65535, "invalid port")
    # The business's own records (exports, pulled aggregates), read as weekly aggregates only.
    if operations_dir is None and repo is not None:
        operations_dir = repo / ".ontologyex" / "operations"
    operations_cache: dict = {}
    check(view_engine in ("python", "rust"), "unsupported graph view backend")
    native_view = native_view_bridge.NativeView() if view_engine == "rust" else None

    def rust_commit(entries: list[dict], ops: list[dict]) -> dict:
        """decisions.commit computed by `ontologyex-view decide` (tests/rust_decide_parity.py checks they agree)."""
        answer = native_view.execute({"execution_authorized": False, "entries": entries, "ops": ops}, "decide")
        if answer.get("refused"):
            raise decisions.Refused(answer["refused"], answer["ids"])
        return {"entries": answer["entries"], "changes": answer["changes"]}

    def rust_decides() -> bool:
        """Whether the installed ontologyex-view has the decision kernel (engine releases before it do not)."""
        try:
            native_view.execute({"execution_authorized": False, "entries": [],
                                 "ops": [{"id": "probe", "expect": None, "set": None}]}, "decide")
            return True
        except native_view_bridge.NativeViewError:
            return False

    # A capability check at start, not a fallback: once the Rust kernel decides, a failure is an error.
    native_commit = rust_commit if native_view is not None and rust_decides() else None
    native_discovery = view_engine == "rust" or native_view_bridge.discovery_engine(discovery_engine) == "rust"
    rules = read_thresholds(thresholds)
    skills = list(skills or [])
    # Fail at startup on a tampered skill or malformed links; re-verified per request.
    if links is not None and links.exists():
        business_view.read_link_document(links)
    if dependencies is not None and dependencies.exists():
        dependency_suggest.read_dependency_document(dependencies)
    check(len(skills) <= business_view.MAX_SKILLS, "too many skills")
    check(history is None or not history.is_symlink(), "--history must not be a symlink")
    check(10 <= history_seconds <= 86_400, "--history-seconds is 10..86400")
    check(not allow_link_decisions or (links is not None and skills) or dependencies is not None or rules_file is not None,
          "decisions need --links and at least one --skill, --dependencies, or --rules")
    if rules_file is not None and rules_file.exists():
        rule_suggest.read_rule_document(rules_file)
    # Per-process secret for the opt-in write route; served only inside /business.
    write_token = secrets.token_urlsafe(32) if allow_link_decisions else None
    # Opt-in decision model (Jev or Clef): proposals and routing only, never decisions.
    decider = decide.DecisionClient() if decision_model else None
    decision_lock = Lock()
    repo_files = watermark.RepoFiles(repo)  # where skills' cited files live, for evidence watermarks
    if repo is not None:
        base_command = ["python", "ontology-extraction/scripts/link_suggest.py", "{decision}",
                        "--repo", str(repo), *[x for path in skills for x in ("--skill", str(path))],
                        "--links", str(links) if links is not None else "LINKS.json",
                        *(["--model-assist"] if decision_model else [])]
    else:
        base_command = None
    calls_path = observations.with_suffix(".calls.json") if observations is not None else None
    dependency_command = (["python", "ontology-extraction/scripts/dependency_suggest.py", "confirm", "--repo",
                           str(repo), "--calls", str(calls_path), "--dependencies",
                           str(dependencies) if dependencies is not None else "DEPENDENCIES.json"]
                          if repo is not None and calls_path is not None else None)

    def page_views(picture: dict, full: dict) -> dict:
        """The fingerprints of what a page lets a person decide, and the version of every decided
        row (what an undo or a re-confirm sends back, so it applies only to the row the page showed)."""
        views = stale_view.views(picture, watermark.Marks(repo_files, skills, full), repo_files)
        for kind, path, read, key in (
                ("links", links, business_view.read_link_document, link_suggest.link_key),
                ("dependencies", dependencies, dependency_suggest.read_dependency_document,
                 dependency_suggest.dependency_key),
                ("rules", rules_file, rule_suggest.read_rule_document, rule_suggest.rule_key)):
            rows = read(path) if path is not None and path.exists() else ([], [])
            views[kind]["entry"] = {e["id"]: decisions.version(e) for e in decisions.entries_of(*rows, key)}
        return views

    def dependency_documents() -> tuple[list[dict], list[dict]]:
        return (dependency_suggest.read_dependency_document(dependencies)
                if dependencies is not None and dependencies.exists() else ([], []))

    def feed_status() -> dict:
        """Summary of feeds.py's status file beside the observations file, if any."""
        path = observations.with_suffix(".status.json") if observations is not None else None
        if path is None or not path.is_file() or path.is_symlink():
            return {"status": "NO_LIVE_FEEDS"}
        try:
            data = parse_json(safe_read(path, 262144))
            rows = data["feeds"]
            check(type(rows) is dict and len(rows) <= 1024, "invalid feed status")
            failing = sorted(name for name, row in rows.items()
                             if type(row) is dict and row.get("ok") is False)[:20]
            return {"status": "LIVE_FEEDS", "feeds": len(rows), "failing": failing,
                    "generated_at": str(data.get("generated_at", ""))[:40],
                    "otlp_listening": data.get("otlp_listening") if type(data.get("otlp_listening")) is str else None}
        except (RoomError, OSError, KeyError, TypeError, ValueError):
            return {"status": "FEED_STATUS_UNREADABLE"}

    def business(full: dict) -> dict:
        link_rows, rejected = ((business_view.read_link_document(links)
                                if links is not None and links.exists() else ([], [])))
        result = business_view.project(skills, full, link_rows, events)
        suggestions = link_suggest.suggest(skills, full, link_rows, rejected, native_view, decider)
        if proposals is not None:  # agent proposals join the same queue; only a person confirms them
            suggestions = link_suggest.merge_proposals(
                suggestions, link_suggest.prepare(skills, full, link_rows, rejected),
                link_suggest.read_proposals(proposals))
        if decider is not None:
            try:
                rows = [row for path in skills for row in evidence_grade.triage(path, decider)["rows"]]
                result = business_view.attach_triage(result, rows)
            except decide.DecisionError as exc:
                result["triage_error"] = str(exc)[:300]
        result["decision_model"] = decide.describe(decider)
        for row in suggestions["suggestions"]:
            row["confirm_command"] = (" ".join(shlex.quote(part) for part in [
                *[x.format(decision="confirm") for x in base_command],
                "--id", row["id"], "--by", "YOUR_NAME"]) if base_command else None)
        result["link_suggestions"] = suggestions
        result["link_decisions"] = ("ENABLED_LOCAL_TOKEN" if write_token and links is not None and skills else
                                    "READ_ONLY_USE_CLI")
        labels = {n["id"]: n["label"] for n in full["nodes"]}
        # What each confirmed link rested on when it was decided, and the same evidence now.
        marks = watermark.Marks(repo_files, skills, full) if link_rows else None
        result["link_watermarks"] = [
            {"id": decisions.decision_id(*link_suggest.link_key(row)), "skill": row["skill"],
             "binding": row["binding"], "component": row["component"], "recorded": row.get("evidence"),
             "now": marks.now(*link_suggest.link_key(row), recorded=row.get("evidence"))} for row in link_rows]
        checks = {(m["skill"], m["binding"], m["component"]): entity_context.evidence_check(m["recorded"], m["now"])
                  for m in result["link_watermarks"]}
        result["link_decided"] = [
            {**row, "component_label": labels.get(row["component"], row["component"]),
             **({"evidence_check": checks[link_suggest.link_key(row)]["status"],
                 "evidence_changed": checks[link_suggest.link_key(row)]["changed"]}
                if row["decision"] == "confirm" else {})}
            for row in decisions.decided(link_rows, rejected, link_suggest.link_key)]
        dep_confirmed, dep_rejected = dependency_documents()
        result["dependency_decided"] = [
            {k: v for k, v in row.items() if k != "evidence"} | {
                "source_label": labels.get(row["source"], row["source"]),
                "target_label": labels.get(row["target"], row["target"])}
            for row in decisions.decided(dep_confirmed, dep_rejected, dependency_suggest.dependency_key)]
        try:
            observed = dependency_suggest.suggest(full, dependency_suggest.read_calls(calls_path),
                                                  dep_confirmed, dep_rejected)
        except ValueError as exc:  # a calls file the feeds are rewriting, or one edited by hand
            observed = {"status": "CALLS_FILE_UNREADABLE", "detail": str(exc)[:200], "suggestions": [],
                        "corroborated": [], "gaps": [], "rejected_not_shown": 0, "execution_authorized": False}
        if proposals is not None:  # agent dependency proposals join the same queue; only a person confirms them
            observed = dependency_suggest.merge_proposals(observed, full, dep_confirmed, dep_rejected,
                                                          dependency_suggest.read_proposals(proposals))
        for row in observed["suggestions"]:
            row["confirm_command"] = (" ".join(shlex.quote(part) for part in [
                *dependency_command, "--id", row["id"], "--by", "YOUR_NAME",
                *(["--proposals", str(proposals)] if proposals is not None and "calls" not in row["evidence"] else [])])
                if dependency_command else None)
        result["dependency_suggestions"] = observed
        # Rules agents proposed, with their cited lines checked, and the ones a person decided.
        rule_rows, rule_rejected = (rule_suggest.read_rule_document(rules_file)
                                    if rules_file is not None and rules_file.exists() else ([], []))
        keys = {row["key"] for row in [*result.get("actions", []), *result.get("objects", [])]}
        result["rule_suggestions"] = rule_suggest.suggest(rule_suggest.read_proposals(proposals), rule_rows,
                                                          rule_rejected, keys, repo_files)
        person_rules = rule_suggest.confirmed_view(rule_rows, repo_files)
        result["rule_decided"] = person_rules + [
            {"id": decisions.decision_id(*rule_suggest.rule_key(r)), "target": r["target"], "statement": r["statement"],
             "decision": "reject", "declared_by": r["declared_by"], "note": r["note"]} for r in rule_rejected]
        for row in [*result.get("actions", []), *result.get("objects", [])]:
            row["person_rules"] = [r for r in person_rules if r["target"] == row["key"]][:20]
            row["rule_proposals"] = [{k: s[k] for k in ("id", "statement", "citation", "citation_check", "proposed_by",
                                                         "signals")}
                                     for s in result["rule_suggestions"]["suggestions"] if s["target"] == row["key"]][:20]
        result["rule_decisions"] = "ENABLED_LOCAL_TOKEN" if write_token and rules_file is not None else "READ_ONLY_USE_CLI"
        result["dependency_decisions"] = ("ENABLED_LOCAL_TOKEN" if write_token and dependencies is not None else
                                          "READ_ONLY_USE_CLI")
        result["feeds"] = feed_status()
        result["discovery_freshness"] = full["discovery_freshness"]
        result["discovery_scanned_at"] = full["discovery_scanned_at"]
        return result
    check(60 <= github_refresh_seconds <= 3600, "invalid GitHub refresh interval")
    check((repo is None) != (github is None), "configure exactly one source")
    if github:
        github_discovery.parse_repository(github)
        client = github_client or github_discovery.GitHubClient(
            token=os.environ.get("ONTOLOGYEX_GITHUB_TOKEN"))
    else:
        client = None
    if snapshot_dir is not None:
        check(github is not None, "snapshot persistence requires --github")
    store = snapshot_store.SnapshotStore(snapshot_dir) if snapshot_dir is not None else None
    state_cache: dict = {}
    cache_lock = RLock()
    remote = (RemoteInventory(lambda: github_discovery.discover_github(github, client),
                              store=store, repository=github)
              if github else None)
    stop_worker = Event()
    worker = None

    if remote is not None:
        def refresh_loop():
            while not stop_worker.is_set():
                remote.refresh()
                stop_worker.wait(github_refresh_seconds)
        worker = Thread(target=refresh_loop, name="ontologyex-github-readonly", daemon=True)
        worker.start()

    scanner = None
    if remote is None:  # the slow reads of the repository run in the background (background_scans.py)
        def scan_nodes() -> list[dict]:
            discovered = (native_view_bridge.native_discover(repo) if native_discovery else discover_repo.discover(repo))
            return with_code_surface(discovered, repo, scanner.result("code", wait=False)[0])["nodes"]
        scanner = background_scans.Scanner(repo, [
            # With the Rust engine, Python, JS/TS and Go are read from syntax trees (tree-sitter).
            ("code", CODE_SURFACE_SECONDS, lambda: code_surface.scan(
                repo, native_view_bridge.engine_binary("ontologyex-discover") if native_discovery else None)),
            ("suppliers", CODE_SURFACE_SECONDS, lambda: suppliers.scan(repo, scan_nodes())),
            ("ownership", OWNERSHIP_SECONDS, lambda: ownership.scan_repo(repo, datetime.now(timezone.utc))),
            ("compliance", OWNERSHIP_SECONDS, lambda: compliance.scan_repo(repo)),
            # Where each skill's cited lines are now: unchanged, moved, reformatted, changed or gone (drift.py).
            ("drift", CODE_SURFACE_SECONDS,
             lambda: drift.skill_drift(skills, watermark.RepoFiles(repo, ttl=0)) if skills else None),
        ])

    def snapshot(observation_rows: list[dict] | None = None) -> dict:
        current = datetime.now(timezone.utc)
        if remote is not None:
            topology, last_success, refresh_error = remote.snapshot()
        else:
            with cache_lock:
                last_attempt = state_cache.get("last_attempt")
                if last_attempt is None or (current - last_attempt).total_seconds() > 15:
                    state_cache["last_attempt"] = current
                    topology = (native_view_bridge.native_discover(repo) if native_discovery
                                else discover_repo.discover(repo))
                    # The slow reads come from the background scans: the last result committed for an
                    # unchanged repository, never a scan run while this request waits (background_scans.py).
                    code, code_error = scanner.result("code")
                    topology = (with_code_surface(topology, repo, code) if code is not None else
                                {**topology, "gaps": [*topology.get("gaps", []),
                                                      {"source": "code", "reason": f"code not scanned ({code_error})"}]})
                    found, _ = scanner.result("suppliers")  # the outside companies, as entities services depend on
                    if found is not None:
                        topology = suppliers.with_suppliers(topology, found, repo)
                    state_cache["topology"] = topology
                    state_cache["last_success"] = current
                topology = state_cache["topology"]
                last_success = state_cache["last_success"]
                refresh_error = False
        if dependencies is not None:
            topology = dependency_suggest.with_confirmed(topology, dependency_documents()[0], dependencies.name)
        result = build_state(topology, (observation_rows if observation_rows is not None
                                        else read_observations(observations)), rules, current)
        result["discovery_source"] = topology.get("source", {"provider": "local"})
        result["discovery_freshness"] = (
            "STALE_LAST_KNOWN" if refresh_error else "AS_OF_LATEST_SUCCESSFUL_SCAN")
        result["discovery_scanned_at"] = (
            last_success.isoformat().replace("+00:00", "Z")
            if last_success is not None else None)
        result["snapshot_history"] = store.history(github) if store is not None else []
        result["durability"] = ("PRIVATE_LOCAL_SQLITE_NOT_BACKUP"
                                if store is not None else "MEMORY_ONLY")
        if refresh_error:
            result["gaps"].append({"source": "github", "reason": "GITHUB_REFRESH_UNAVAILABLE"})
        return result

    def supplier_report() -> dict:
        """The supplier map with what relies on each supplier, from the same model as every page."""
        report = scanner.result("suppliers")[0] if scanner is not None else None
        return report if report is not None else {"schema_version": 1, "kind": "SUPPLIER_MAP", "suppliers": [],
                                                  "categories": [], "own_supplier_records": [],
                                                  "status": "NO_LOCAL_REPOSITORY" if remote is not None else "NOT_SCANNED",
                                                  "execution_authorized": False}

    def model() -> dict:
        """What the entity context reads: the state, the business picture, and the decision history."""
        full = snapshot()
        return {"state": full, "business": business(full) if skills or dependencies is not None else {},
                "link_history": entity_context.read_history(
                    decisions.history_path(links) if links is not None else None),
                "dependency_history": entity_context.read_history(
                    decisions.history_path(dependencies) if dependencies is not None else None),
                "agent_proposals": link_suggest.read_proposals(proposals),
                "agent_dependency_proposals": dependency_suggest.read_proposals(proposals),
                "suppliers": supplier_report()}

    def operations_report(data: dict) -> dict:
        """The operations ledger, read again only when a file in the folder or the week changed;
        compared each time with the model and code as they are now."""
        now = datetime.now(timezone.utc)
        entities = list(entity_context._entities(data).values())
        names = tuple(sorted(e.get("label") or "" for e in entities if e.get("layer") == "business"))
        key = (operations.week_of(now), operations.folder_revision(operations_dir), names)
        if operations_cache.get("key") != key:  # ticket subjects are matched against the business items' names
            operations_cache["key"], operations_cache["report"] = key, operations.report(operations_dir, entities,
                                                                                         now=now)
        found = operations.with_live(operations_cache["report"], operations_dir, now)
        return operations.with_comparison(found, entities, (data.get("suppliers") or {}).get("suppliers"))

    def bundle() -> dict:
        """The model, with who owns each part of it."""
        data = model()
        data["ownership"] = ownership.report(data, scanner.result("ownership")[0] if scanner is not None else None)
        data["compliance"] = compliance.report(data, scanner.result("compliance")[0] if scanner is not None else None)
        data["operations"] = operations_report(data)
        data["guardrails"] = guardrail.summary(repo, datetime.now(timezone.utc))  # agent calls the rules stopped
        data["drift"] = drift.assemble(scanner.result("drift")[0] if scanner is not None else None,
                                       drift.rules_drift(rules_file, repo_files))
        return data

    if print_bundle:  # the same assembly the server answers from, for tests, parity and agents offline
        if scanner is not None:
            scanner.run_pending()  # one pass, here and now: there is no server to wait for
        print(json.dumps(bundle(), sort_keys=True, separators=(",", ":")))
        return

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
            route = urlsplit(self.path)
            if len(self.path) > 2200 or route.fragment:
                payload, mime, code = b"Request rejected", "text/plain", 414
            elif route.path == "/" and not route.query:
                payload, mime, code = PAGE.encode("utf-8"), "text/html; charset=utf-8", 200
            elif route.path == "/business" and not route.query:
                page = (Path(__file__).resolve().parent / "operating_picture.html").read_text(encoding="utf-8")
                page = page.replace('<meta name="ontologyex-write-token" content=""/>',
                                    f'<meta name="ontologyex-write-token" content="{write_token}"/>'
                                    if write_token else '<meta name="ontologyex-write-token" content=""/>')
                payload, mime, code = page.encode("utf-8"), "text/html; charset=utf-8", 200
            elif route.path == "/api/ask":
                mime = "application/json; charset=utf-8"
                try:
                    params = parse_qs(route.query, strict_parsing=True, max_num_fields=1)
                    query = params.get("q", [""])
                    if set(params) != {"q"} or len(query) != 1 or not 0 < len(query[0]) <= 200 \
                            or not query[0].isprintable():
                        raise graph_view_api.ViewError("ask takes one printable q of 1..200 characters")
                    picture = business(snapshot())
                    try:
                        answer = business_view.route_command(picture, query[0], decider)
                    except decide.DecisionError:
                        answer = {**business_view.keyword_route(picture, query[0]),
                                  "router": "keywords (decision model unavailable)"}
                    payload, code = json.dumps({"kind": "COMMAND_ROUTE", **answer}, sort_keys=True).encode(), 200
                except (graph_view_api.ViewError, ValueError):
                    payload, code = b'{"status":"INVALID_QUERY"}', 400
                except (RoomError, DomainError, discover_repo.DiscoveryError,
                        github_discovery.GitHubDiscoveryError, OSError, TypeError, KeyError):
                    payload, code = b'{"status":"SOURCE_UNAVAILABLE"}', 503
            elif route.path == "/api/business":
                mime = "application/json; charset=utf-8"
                try:
                    if route.query:
                        raise graph_view_api.ViewError("business view takes no parameters")
                    full = snapshot()
                    answer = business(full)  # with the fingerprints of what it lets a person decide, from the same state
                    answer["views"] = page_views(answer, full)
                    payload, code = json.dumps(answer, sort_keys=True).encode(), 200
                except graph_view_api.ViewError:
                    payload, code = b'{"status":"INVALID_QUERY"}', 400
                except (RoomError, DomainError, discover_repo.DiscoveryError,
                        github_discovery.GitHubDiscoveryError, OSError, TypeError, KeyError):
                    payload, code = b'{"status":"SOURCE_UNAVAILABLE"}', 503
            elif route.path == "/api/scans":  # what each background scan last did, and for which revision
                mime = "application/json; charset=utf-8"
                if route.query:
                    payload, code = b'{"status":"INVALID_QUERY"}', 400
                elif scanner is None:
                    payload, code = b'{"status":"NO_LOCAL_REPOSITORY","scans":[]}', 200
                else:
                    payload, code = json.dumps(scanner.status(), sort_keys=True).encode(), 200
            elif route.path in ("/api/links/batches", "/api/dependencies/batches", "/api/rules/batches"):
                # The recorded decision batches, newest first: who, when, what, and whether undone.
                mime = "application/json; charset=utf-8"
                path = {"links": links, "dependencies": dependencies, "rules": rules_file}[route.path.split("/")[2]]
                if route.query:
                    payload, code = b'{"status":"INVALID_QUERY"}', 400
                else:
                    try:
                        found = decisions.batches(path) if path is not None else []
                        payload, code = json.dumps({"schema_version": 1, "kind": "DECISION_BATCHES",
                                                    "batches": found[:50], "execution_authorized": False},
                                                   sort_keys=True).encode(), 200
                    except OSError:
                        payload, code = b'{"status":"SOURCE_UNAVAILABLE"}', 503
            elif route.path == "/api/views":  # fingerprints of what a page lets a person decide; read before the page
                mime = "application/json; charset=utf-8"
                try:
                    if route.query:
                        raise graph_view_api.ViewError("views takes no parameters")
                    full = snapshot()
                    answer = page_views(business(full), full)
                    payload, code = json.dumps(answer, sort_keys=True).encode(), 200
                except graph_view_api.ViewError:
                    payload, code = b'{"status":"INVALID_QUERY"}', 400
                except (RoomError, DomainError, discover_repo.DiscoveryError,
                        github_discovery.GitHubDiscoveryError, OSError, TypeError, KeyError):
                    payload, code = b'{"status":"SOURCE_UNAVAILABLE"}', 503
            elif route.path == "/entity":
                page = (Path(__file__).resolve().parent / "entity_page.html").read_text(encoding="utf-8")
                page = page.replace('<meta name="ontologyex-write-token" content=""/>',
                                    f'<meta name="ontologyex-write-token" content="{write_token}"/>'
                                    if write_token else '<meta name="ontologyex-write-token" content=""/>')
                payload, mime, code = page.encode("utf-8"), "text/html; charset=utf-8", 200
            elif route.path == "/api/bundle":  # what agent_tools.py reads: the same model as the pages
                mime = "application/json; charset=utf-8"
                try:
                    if route.query:
                        raise graph_view_api.ViewError("bundle takes no parameters")
                    payload, code = json.dumps(bundle(), sort_keys=True).encode(), 200
                except graph_view_api.ViewError:
                    payload, code = b'{"status":"INVALID_QUERY"}', 400
                except (RoomError, DomainError, discover_repo.DiscoveryError,
                        github_discovery.GitHubDiscoveryError, OSError, TypeError, KeyError):
                    payload, code = b'{"status":"SOURCE_UNAVAILABLE"}', 503
            elif route.path == "/suppliers" and not route.query:
                page = (Path(__file__).resolve().parent / "suppliers_page.html").read_bytes()
                payload, mime, code = page, "text/html; charset=utf-8", 200
            elif route.path == "/api/suppliers":  # each supplier with what relies on it
                mime = "application/json; charset=utf-8"
                try:
                    if route.query:
                        raise graph_view_api.ViewError("suppliers takes no parameters")
                    data = bundle()
                    report = dict(data["suppliers"])
                    known = entity_context.entity_ids(data)
                    report["suppliers"] = [
                        {**row, "relied_on_by": entity_context.context(data, row["id"])["relied_on_by"]}
                        if row["id"] in known else row for row in report["suppliers"]]
                    payload, code = json.dumps(report, sort_keys=True).encode(), 200
                except (graph_view_api.ViewError, entity_context.ContextError):
                    payload, code = b'{"status":"INVALID_QUERY"}', 400
                except (RoomError, DomainError, discover_repo.DiscoveryError,
                        github_discovery.GitHubDiscoveryError, OSError, TypeError, KeyError):
                    payload, code = b'{"status":"SOURCE_UNAVAILABLE"}', 503
            elif route.path == "/command" and not route.query:
                page = (Path(__file__).resolve().parent / "command_page.html").read_bytes()
                payload, mime, code = page, "text/html; charset=utf-8", 200
            elif route.path == "/api/command":  # everything that needs attention, ranked, with why
                mime = "application/json; charset=utf-8"
                try:
                    if route.query:
                        raise graph_view_api.ViewError("command takes no parameters")
                    payload, code = json.dumps(command_center.report(bundle()), sort_keys=True).encode(), 200
                except (graph_view_api.ViewError, entity_context.ContextError):
                    payload, code = b'{"status":"INVALID_QUERY"}', 400
                except (RoomError, DomainError, discover_repo.DiscoveryError,
                        github_discovery.GitHubDiscoveryError, OSError, TypeError, KeyError):
                    payload, code = b'{"status":"SOURCE_UNAVAILABLE"}', 503
            elif route.path == "/compliance":  # the pack in the query is read by the page
                page = (Path(__file__).resolve().parent / "compliance_page.html").read_bytes()
                payload, mime, code = page, "text/html; charset=utf-8", 200
            elif route.path == "/api/drift":  # where the lines the model cites are now
                mime = "application/json; charset=utf-8"
                try:
                    params = parse_qs(route.query, strict_parsing=True, max_num_fields=2) if route.query else {}
                    entity, shape = params.get("id", [None]), params.get("format", ["json"])
                    if not set(params) <= {"id", "format"} or len(entity) != 1 or len(shape) != 1 \
                            or shape[0] not in ("json", "markdown") or (entity[0] is not None and len(entity[0]) > 300):
                        raise graph_view_api.ViewError("drift takes id and format")
                    answer = bundle()["drift"]
                    if entity[0] is not None:  # one business item: its state and the spans it cites
                        mine = [i for i in answer["items"] if i["id"] == entity[0]]
                        cited = {(i["skill"], e) for i in mine for e in i["evidence"]}
                        answer = {**answer, "items": mine,
                                  "spans": [x for x in answer["spans"] if (x["skill"], x["evidence"]) in cited],
                                  "rules": [r for r in answer["rules"] if r["target"] == entity[0]]}
                    if shape[0] == "markdown":
                        payload, mime, code = drift.markdown(answer).encode(), "text/markdown; charset=utf-8", 200
                    else:
                        payload, code = json.dumps(answer, sort_keys=True).encode(), 200
                except graph_view_api.ViewError:
                    payload, code = b'{"status":"INVALID_QUERY"}', 400
                except (RoomError, DomainError, discover_repo.DiscoveryError,
                        github_discovery.GitHubDiscoveryError, OSError, TypeError, KeyError):
                    payload, code = b'{"status":"SOURCE_UNAVAILABLE"}', 503
            elif route.path == "/api/compliance":  # what each pack asks for and what the sources show
                mime = "application/json; charset=utf-8"
                try:
                    params = parse_qs(route.query, strict_parsing=True, max_num_fields=2) if route.query else {}
                    pack, shape = params.get("pack", [None]), params.get("format", ["json"])
                    if not set(params) <= {"pack", "format"} or len(pack) != 1 or len(shape) != 1 \
                            or (pack[0] is not None and pack[0] not in compliance.PACKS) \
                            or shape[0] not in ("json", "markdown"):
                        raise graph_view_api.ViewError("compliance takes pack and format")
                    answer = bundle()["compliance"]
                    if shape[0] == "markdown":
                        payload, mime, code = (compliance.markdown(answer, pack[0]).encode(),
                                               "text/markdown; charset=utf-8", 200)
                    else:
                        if pack[0]:
                            answer = {**answer, "packs": [p for p in answer["packs"] if p["id"] == pack[0]]}
                        payload, code = json.dumps(answer, sort_keys=True).encode(), 200
                except graph_view_api.ViewError:
                    payload, code = b'{"status":"INVALID_QUERY"}', 400
                except (RoomError, DomainError, discover_repo.DiscoveryError,
                        github_discovery.GitHubDiscoveryError, OSError, TypeError, KeyError):
                    payload, code = b'{"status":"SOURCE_UNAVAILABLE"}', 503
            elif route.path == "/operations" and not route.query:
                page = (Path(__file__).resolve().parent / "operations_page.html").read_bytes()
                payload, mime, code = page, "text/html; charset=utf-8", 200
            elif route.path == "/api/operations":  # the business's own records, as weekly aggregates
                mime = "application/json; charset=utf-8"
                try:
                    params = parse_qs(route.query, strict_parsing=True, max_num_fields=1) if route.query else {}
                    shape = params.get("format", ["json"])
                    if not set(params) <= {"format"} or len(shape) != 1 or shape[0] not in ("json", "markdown"):
                        raise graph_view_api.ViewError("operations takes format")
                    answer = bundle()["operations"]
                    if shape[0] == "markdown":
                        payload, mime = operations.markdown(answer).encode(), "text/markdown; charset=utf-8"
                    else:
                        payload = json.dumps(answer, sort_keys=True).encode()
                    code = 200
                except graph_view_api.ViewError:
                    payload, code = b'{"status":"INVALID_QUERY"}', 400
                except (RoomError, DomainError, discover_repo.DiscoveryError, operations.OperationsError,
                        github_discovery.GitHubDiscoveryError, OSError, TypeError, KeyError):
                    payload, code = b'{"status":"SOURCE_UNAVAILABLE"}', 503
            elif route.path == "/owners" and not route.query:
                page = (Path(__file__).resolve().parent / "owners_page.html").read_bytes()
                payload, mime, code = page, "text/html; charset=utf-8", 200
            elif route.path == "/api/owners":  # who owns each entity: CODEOWNERS, then git history
                mime = "application/json; charset=utf-8"
                try:
                    params = parse_qs(route.query, strict_parsing=True, max_num_fields=1) if route.query else {}
                    if not set(params) <= {"id"} or any(len(v) != 1 for v in params.values()):
                        raise graph_view_api.ViewError("owners takes an optional id")
                    answer = bundle()["ownership"]
                    if "id" in params:
                        found = [r for r in answer["entities"] if r["id"] == params["id"][0]]
                        answer = {**answer, "entities": found, "people": []}
                    payload, code = json.dumps(answer, sort_keys=True).encode(), 200
                except graph_view_api.ViewError:
                    payload, code = b'{"status":"INVALID_QUERY"}', 400
                except (RoomError, DomainError, discover_repo.DiscoveryError,
                        github_discovery.GitHubDiscoveryError, OSError, TypeError, KeyError):
                    payload, code = b'{"status":"SOURCE_UNAVAILABLE"}', 503
            elif route.path == "/digest":  # days in the query are read by the page
                page = (Path(__file__).resolve().parent / "digest_page.html").read_bytes()
                payload, mime, code = page, "text/html; charset=utf-8", 200
            elif route.path == "/api/digest":  # what moved since the window began, from the --history file
                mime = "application/json; charset=utf-8"
                try:
                    params = parse_qs(route.query, strict_parsing=True, max_num_fields=2) if route.query else {}
                    days = params.get("days", ["7"])
                    shape = params.get("format", ["json"])
                    if not set(params) <= {"days", "format"} or len(days) != 1 or len(shape) != 1 \
                            or not days[0].isdigit() or not 1 <= int(days[0]) <= digest.MAX_DAYS \
                            or shape[0] not in ("json", "markdown"):
                        raise graph_view_api.ViewError(f"digest takes days 1..{digest.MAX_DAYS} and format")
                    data, now = bundle(), datetime.now(timezone.utc)
                    window = int(days[0])
                    answer = digest.build(
                        digest.read_history(history), command_center.report(data), now, window,
                        digest.decision_logs(links, dependencies, rules_file),
                        digest._code(repo.resolve(), data, now - timedelta(days=window)) if repo is not None else None,
                        digest.labels(data))
                    answer["recording"] = str(history.name) if history is not None else None
                    if shape[0] == "markdown":
                        payload, mime, code = digest.markdown(answer).encode(), "text/markdown; charset=utf-8", 200
                    else:
                        payload, code = json.dumps(answer, sort_keys=True).encode(), 200
                except (graph_view_api.ViewError, entity_context.ContextError):
                    payload, code = b'{"status":"INVALID_QUERY"}', 400
                except (RoomError, DomainError, discover_repo.DiscoveryError,
                        github_discovery.GitHubDiscoveryError, OSError, TypeError, KeyError):
                    payload, code = b'{"status":"SOURCE_UNAVAILABLE"}', 503
            elif route.path == "/impact":  # base and head in the query are read by the page
                page = (Path(__file__).resolve().parent / "impact_page.html").read_bytes()
                payload, mime, code = page, "text/html; charset=utf-8", 200
            elif route.path == "/api/impact":  # what the working tree or a branch changes, and what relies on it
                mime = "application/json; charset=utf-8"
                try:
                    params = parse_qs(route.query, strict_parsing=True, max_num_fields=2) if route.query else {}
                    if not set(params) <= {"base", "head"} or any(len(v) != 1 for v in params.values()):
                        raise graph_view_api.ViewError("impact takes an optional base and head")
                    if repo is None:
                        raise change_impact.DiffError("change impact needs a local --repo")
                    changes, omitted = change_impact.git_changes(
                        repo.resolve(), params.get("base", [None])[0], params.get("head", [None])[0])
                    data = bundle()
                    answer = (native_view.execute({**data, "changes": changes}, "change-impact")
                              if native_view is not None else entity_context.change_impact(data, changes))
                    payload, code = json.dumps({**answer, "files_omitted": omitted}, sort_keys=True).encode(), 200
                except change_impact.DiffError as exc:
                    payload, code = json.dumps({"status": "DIFF_UNAVAILABLE", "error": str(exc)[:300]}).encode(), 400
                except (graph_view_api.ViewError, entity_context.ContextError, ValueError,
                        native_view_bridge.NativeViewError):
                    payload, code = b'{"status":"INVALID_QUERY"}', 400
                except (RoomError, DomainError, discover_repo.DiscoveryError,
                        github_discovery.GitHubDiscoveryError, OSError, TypeError, KeyError):
                    payload, code = b'{"status":"SOURCE_UNAVAILABLE"}', 503
            elif route.path == "/api/explain":
                mime = "application/json; charset=utf-8"
                try:
                    params = parse_qs(route.query, strict_parsing=True, max_num_fields=2)
                    if set(params) != {"from", "to"} or any(len(v) != 1 for v in params.values()):
                        raise graph_view_api.ViewError("explain takes one from and one to")
                    data = bundle()
                    source, target = params["from"][0], params["to"][0]
                    if native_view is not None:
                        known = entity_context.entity_ids(data)
                        if source not in known or target not in known:
                            raise entity_context.ContextError("entity not found")
                        answer = native_view.execute(data, "explain", source, target)
                    else:
                        answer = entity_context.explain(data, source, target)
                    payload, code = json.dumps(answer, sort_keys=True).encode(), 200
                except entity_context.ContextError as exc:
                    payload, code = json.dumps({"status": "NOT_FOUND", "error": str(exc)[:200]}).encode(), 404
                except (graph_view_api.ViewError, ValueError, native_view_bridge.NativeViewError):
                    payload, code = b'{"status":"INVALID_QUERY"}', 400
                except (RoomError, DomainError, discover_repo.DiscoveryError,
                        github_discovery.GitHubDiscoveryError, OSError, TypeError, KeyError):
                    payload, code = b'{"status":"SOURCE_UNAVAILABLE"}', 503
            elif route.path in ("/api/context", "/api/search"):
                mime = "application/json; charset=utf-8"
                try:
                    params = parse_qs(route.query, strict_parsing=True, max_num_fields=1)
                    name = "id" if route.path == "/api/context" else "q"
                    value = params.get(name, [""])
                    if set(params) != {name} or len(value) != 1:
                        raise graph_view_api.ViewError(f"takes one {name}")
                    data = bundle()
                    if native_view is not None:  # the Rust engine, checked against Python by rust_context_parity
                        if name == "id" and value[0] not in entity_context.entity_ids(data):
                            raise entity_context.ContextError("entity not found")
                        answer = native_view.execute(data, "context" if name == "id" else "search", value[0])
                    else:
                        answer = (entity_context.context(data, value[0]) if name == "id"
                                  else entity_context.search(data, value[0]))
                    payload, code = json.dumps(answer, sort_keys=True).encode(), 200
                except entity_context.ContextError as exc:
                    payload = json.dumps({"status": "NOT_FOUND" if "not found" in str(exc) else "INVALID_QUERY",
                                          "error": str(exc)[:200]}).encode()
                    code = 404 if "not found" in str(exc) else 400
                except (graph_view_api.ViewError, ValueError, native_view_bridge.NativeViewError):
                    payload, code = b'{"status":"INVALID_QUERY"}', 400
                except (RoomError, DomainError, discover_repo.DiscoveryError,
                        github_discovery.GitHubDiscoveryError, OSError, TypeError, KeyError):
                    payload, code = b'{"status":"SOURCE_UNAVAILABLE"}', 503
            elif route.path == "/static/layout_engine.js" and not route.query:
                payload = (Path(__file__).resolve().parent / "layout_engine.js").read_bytes()
                mime, code = "application/javascript; charset=utf-8", 200
            elif route.path in ("/api/state", "/api/view", "/api/node", "/api/neighborhood", "/api/incident"):
                mime = "application/json; charset=utf-8"
                try:
                    params = parse_qs(route.query, strict_parsing=True, keep_blank_values=True,
                                      max_num_fields=4)
                    if any(len(values) != 1 for values in params.values()):
                        raise graph_view_api.ViewError("ambiguous query parameter")
                    full = snapshot()
                    if route.path == "/api/state":
                        if params:
                            raise graph_view_api.ViewError("summary takes no query parameters")
                        result = (native_view.execute(full, "summary") if native_view
                                  else graph_view_api.summary(full))
                    elif route.path == "/api/view":
                        if not set(params).issubset({"lens", "q", "offset"}):
                            raise graph_view_api.ViewError("unsupported view parameter")
                        lens = graph_view_api.valid_lens(params.get("lens", ["all"])[0])
                        q = graph_view_api.valid_query(params.get("q", [""])[0])
                        off = graph_view_api.parse_offset(params.get("offset", ["0"])[0])
                        result = (native_view.execute(full, "page", lens, q, str(off))
                                  if native_view else graph_view_api.page(
                                      full, lens=lens, query=q, offset=off))
                    elif route.path == "/api/neighborhood":
                        if set(params) != {"id", "depth"}:
                            raise graph_view_api.ViewError("neighborhood requires id and depth")
                        node_id = params["id"][0]
                        depth = graph_view_api.valid_depth(params["depth"][0])
                        result = (native_view.execute(full, "neighborhood", node_id, str(depth))
                                  if native_view else graph_view_api.neighborhood(
                                      full, node_id, depth=depth))
                    elif route.path == "/api/incident":
                        if set(params) != {"id"}:
                            raise graph_view_api.ViewError("incident requires exact node id")
                        rows = read_observations(observations)
                        full = snapshot(rows)  # same rows for status and timeline
                        result = (native_view.execute(
                            full, "incident", params["id"][0], observation_rows=rows)
                                  if native_view else incident_view.incident(
                                      full, rows, params["id"][0]))
                    else:
                        if set(params) != {"id"}:
                            raise graph_view_api.ViewError("node requires an exact id")
                        result = (native_view.execute(full, "node", params["id"][0])
                                  if native_view else graph_view_api.node_detail(full, params["id"][0]))
                    payload = json.dumps(result, sort_keys=True).encode()
                    code = 200
                except (graph_view_api.ViewError, incident_view.IncidentError, ValueError, UnicodeError):
                    payload, code = b'{"status":"INVALID_QUERY"}', 400
                except (RoomError, discover_repo.DiscoveryError, github_discovery.GitHubDiscoveryError,
                        native_view_bridge.NativeViewError, OSError, TypeError):
                    payload, code = b'{"status":"SOURCE_UNAVAILABLE"}', 503
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
                             "script-src 'self' 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self):
            """The write routes: an explicit link or dependency decision, only when opted in."""
            host_headers = self.headers.get_all("Host", [])
            origin = self.headers.get("Origin")
            route = urlsplit(self.path)
            if (len(host_headers) != 1 or origin is None or not valid_local_host(
                    host_headers[0], origin, self.server.server_port)):
                code, body = 403, {"status": "FORBIDDEN"}
            elif route.path not in ("/api/links/decision", "/api/dependencies/decision",
                                    "/api/rules/decision") or route.query:
                code, body = 404, {"status": "NOT_FOUND"}
            elif write_token is None:
                code, body = 403, {"status": "LINK_DECISIONS_DISABLED"}
            elif route.path == "/api/links/decision" and (links is None or not skills):
                code, body = 403, {"status": "LINK_DECISIONS_DISABLED"}
            elif route.path == "/api/dependencies/decision" and dependencies is None:
                code, body = 403, {"status": "DEPENDENCY_DECISIONS_DISABLED"}
            elif route.path == "/api/rules/decision" and rules_file is None:
                code, body = 403, {"status": "RULE_DECISIONS_DISABLED"}
            elif not hmac.compare_digest(self.headers.get("X-OntologyEX-Token", ""), write_token):
                code, body = 403, {"status": "INVALID_TOKEN"}
            elif self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                code, body = 415, {"status": "JSON_REQUIRED"}
            else:
                try:
                    length = int(self.headers.get("Content-Length", "-1"))
                    if not 0 < length <= 8192:
                        raise ValueError("invalid body length")
                    request = json.loads(self.rfile.read(length),
                                         object_pairs_hook=no_duplicates)
                    # One suggestion ("id") or a batch ("ids"); undo takes decided IDs; "undo_batch" takes
                    # a recorded batch ID. "seen" carries the fingerprints the page showed (for undo, the
                    # versions of the decided rows), so a decision applies only to what the person saw.
                    # "preview": true computes the batch and writes nothing; "plan" (the preview's digests)
                    # applies it only if it is still exactly that batch.
                    seen = request.pop("seen", None) if type(request) is dict else None
                    preview = request.pop("preview", False) if type(request) is dict else False
                    plan = request.pop("plan", None) if type(request) is dict else None
                    if type(request) is not dict or set(request) not in (
                            {"id", "decision", "by", "note"}, {"ids", "decision", "by", "note"},
                            {"batch", "decision", "by", "note"}):
                        raise ValueError("expected id, ids or batch, decision, by and note")
                    if seen is not None and not stale_view.valid_seen(seen):
                        raise ValueError("seen maps IDs to the fingerprints the page showed")
                    if preview is not False and preview is not True:
                        raise ValueError("preview is true or absent")
                    if plan is not None and not (stale_view.valid_seen(plan) and plan and not preview):
                        raise ValueError("plan maps IDs to the digests a preview returned")
                    if "batch" in request:
                        if request["decision"] != "undo_batch" or type(request["batch"]) is not str \
                                or not 0 < len(request["batch"]) <= 64:
                            raise ValueError("a batch can only be undone, by its ID")
                        ids = []
                    else:
                        ids = [request["id"]] if "id" in request else request["ids"]
                        if (type(ids) is not list or not 0 < len(ids) <= decisions.MAX_BATCH
                                or not all(type(i) is str for i in ids)
                                or request["decision"] not in ("confirm", "reject", "undo", "reconfirm")):
                            raise ValueError("invalid IDs or decision")
                    links_route = route.path == "/api/links/decision"
                    rules_route = route.path == "/api/rules/decision"
                    if request["decision"] == "reconfirm" and not (links_route or rules_route):
                        raise ValueError("only links and rules carry evidence watermarks")
                    apply = (link_suggest.apply_decisions if links_route else
                             rule_suggest.apply_decisions if rules_route else dependency_suggest.apply_decisions)
                    target = links if links_route else rules_file if rules_route else dependencies
                    kind = "links" if links_route else "rules" if rules_route else "dependencies"
                    options = {"preview": preview, "plan_digests": plan, "engine": native_commit}
                    with decision_lock:
                        # One reading of the state, for the stale-view check and for what the decision records.
                        full = snapshot()
                        picture = business(full) if request["decision"] in ("confirm", "reject") or seen else None
                        marks = watermark.Marks(repo_files, skills, full)
                        if seen is not None and request["decision"] in ("undo", "undo_batch"):
                            options["expect"] = seen  # the versions of the decided rows the page showed
                        elif seen is not None:
                            stale_view.check(seen, ids, stale_view.views(picture, marks, repo_files)[kind][
                                "decided" if request["decision"] == "reconfirm" else "suggestion"])
                        if request["decision"] == "undo_batch":
                            body = apply(target, request["by"], request["note"], undo_batch=request["batch"], **options)
                        elif request["decision"] == "undo":
                            body = apply(target, request["by"], request["note"], undo=ids, **options)
                        elif request["decision"] == "reconfirm" and rules_route:
                            confirmed_rules = (rule_suggest.read_rule_document(rules_file)[0]
                                               if rules_file.exists() else [])
                            body = rule_suggest.apply_decisions(target, request["by"], request["note"],
                                                                reconfirm=rule_suggest.reconfirm_rows(
                                                                    confirmed_rules, ids, repo_files), **options)
                        elif request["decision"] == "reconfirm":  # a person confirms again what changed
                            confirmed = {decisions.decision_id(*link_suggest.link_key(r)): r
                                         for r in business_view.read_link_document(links)[0]}
                            if not all(i in confirmed for i in ids):
                                raise ValueError("no confirmed link has that ID")
                            body = link_suggest.reconfirm(target, request["by"], request["note"], [
                                {**confirmed[i], "evidence": marks.now(*link_suggest.link_key(confirmed[i]))}
                                for i in dict.fromkeys(ids)], **options)
                        else:
                            current = {x["id"]: x for x in picture[
                                "link_suggestions" if links_route else "rule_suggestions" if rules_route
                                else "dependency_suggestions"]["suggestions"]}
                            if not all(i in current for i in ids):
                                raise ValueError("no current suggestion has that ID")
                            chosen = [current[i] for i in dict.fromkeys(ids)]
                            if links_route and request["decision"] == "confirm":  # record what it rested on
                                chosen = [{**c, "evidence": marks.now(c["skill"], c["binding"], c["component"])}
                                          for c in chosen]
                            body = apply(target, request["by"], request["note"], **{request["decision"]: chosen},
                                         **options)
                    code = 200
                except stale_view.StaleView as exc:  # nothing written: the person decides again on what is there now
                    code, body = 409, {"status": "STALE_VIEW", "changed": exc.changed[:50],
                                       "detail": "What you saw changed since the page loaded. Reload and decide again."}
                except native_view_bridge.NativeViewError:  # the Rust kernel failed: nothing written
                    code, body = 503, {"status": "ENGINE_UNAVAILABLE"}
                except decisions.Refused as exc:  # a stale approval or preview: nothing written
                    code, body = 409, {"status": "STALE_VIEW", "changed": exc.ids[:50], "reason": exc.reason,
                                       "detail": "What you approved changed since you saw it. Nothing was saved. "
                                                 "Look again, then decide."}
                except (ValueError, TypeError, KeyError, UnicodeError, RoomError, DomainError):
                    code, body = 400, {"status": "INVALID_DECISION"}
                except OSError:
                    code, body = 503, {"status": "SOURCE_UNAVAILABLE"}
            payload = json.dumps(body, sort_keys=True).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            return  # No user-supplied paths or request logs persisted.

    recorder = None
    if history is not None:  # opt-in: the ranking over time, for the weekly digest
        def record_loop():
            while not stop_worker.is_set():
                try:
                    digest.record(history, command_center.report(bundle()), datetime.now(timezone.utc))
                except (RoomError, DomainError, discover_repo.DiscoveryError, github_discovery.GitHubDiscoveryError,
                        OSError, TypeError, KeyError, ValueError):
                    pass  # a source that is down now is recorded on the next round
                stop_worker.wait(history_seconds)
        recorder = Thread(target=record_loop, name="ontologyex-history", daemon=True)

    puller = None
    if live_pulls:  # opt-in: the read-only API pulls declared in pulls.json, each on its own interval
        check(operations_dir is not None, "--pulls needs --repo or --operations")
        pulls.load(operations_dir)  # refuse a bad pulls.json before starting
        puller = pulls.Puller(operations_dir, entities=lambda: list(entity_context._entities(model()).values()))

    # No external listener. The only writes are the opt-in decisions, history and pulled aggregates above.
    try:
        with ThreadingHTTPServer(("127.0.0.1", port), Handler) as server:
            print(f"OntologyEX local control room: http://127.0.0.1:{server.server_port}",
                  flush=True)
            if repo is not None:
                print(f"Repository discovery: {'rust' if native_discovery else 'python'}", flush=True)
            if native_view is not None and (links is not None or dependencies is not None or rules_file is not None):
                print("Decisions: " + ("rust (ontologyex-view decide)" if native_commit else
                                       "python (this ontologyex-view predates the decision kernel; "
                                       "update it with fetch_engine.py)"), flush=True)
            if scanner is not None:
                scanner.start()
            if recorder is not None:
                recorder.start()
                print(f"Recording the ranking for the weekly digest: {history}", flush=True)
            if puller is not None:
                puller.start()
                print(f"Live pulls: {len(pulls.load(operations_dir))} declared in {operations_dir / pulls.CONFIG}",
                      flush=True)
            server.serve_forever()
    finally:
        stop_worker.set()
        if scanner is not None:
            scanner.stop()
        if puller is not None:
            puller.stop()
        for thread in (worker, recorder):
            if thread is not None and thread.is_alive():
                thread.join(timeout=2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--repo", type=Path, help="existing local repository")
    source.add_argument("--github", help="GitHub owner/name; optional token via env")
    parser.add_argument("--github-refresh-seconds", type=int, default=300)
    parser.add_argument("--snapshot-dir", type=Path,
                        help="opt-in private server-side snapshot directory (0700)")
    parser.add_argument("--observations", type=Path, default=None)
    parser.add_argument("--thresholds", type=Path, default=None)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--state-only", action="store_true")
    parser.add_argument("--bundle-only", action="store_true",
                        help="print the entity-context bundle (state, business picture, decision history) and exit")
    parser.add_argument("--view-engine", choices=("python", "rust"), default="python",
                        help="read-only projection backend; rust requires compiled ontologyex-view")
    parser.add_argument("--discovery-engine", choices=("auto", "python", "rust"), default="auto",
                        help="repository discovery: auto uses the byte-identical Rust ontologyex-discover "
                             "when it is installed (fetch_engine.py or a cargo build), else Python")
    parser.add_argument("--skill", type=Path, action="append", default=[],
                        help="compiled domain skill for the business view; repeat for more")
    parser.add_argument("--links", type=Path, default=None,
                        help="operator-declared binding-to-component links (JSON)")
    parser.add_argument("--events", type=Path, default=None,
                        help="evidence grade events file (see evidence_grade.py)")
    parser.add_argument("--dependencies", type=Path, default=None,
                        help="dependencies confirmed from observed calls (JSON; see dependency_suggest.py)")
    parser.add_argument("--rules", type=Path, default=None,
                        help="business rules a person decided from agent proposals (JSON; see rule_suggest.py)")
    parser.add_argument("--proposals", type=Path, default=None,
                        help="agent link proposals (JSON lines written by agent_tools.py); shown as suggestions")
    parser.add_argument("--history", type=Path, default=None,
                        help="append the command-center ranking to this JSON-lines file when it changes "
                             "(and once a day), for the weekly digest at /digest")
    parser.add_argument("--history-seconds", type=int, default=300, help="how often to check the ranking")
    parser.add_argument("--operations", type=Path, default=None,
                        help="the business's own records: exports and pulled aggregates, read as weekly "
                             "aggregates only (default: REPO/.ontologyex/operations)")
    parser.add_argument("--pulls", action="store_true",
                        help="run the read-only API pulls declared in the operations folder's pulls.json, each on "
                             "its interval (keys from environment variables only)")
    parser.add_argument("--allow-link-decisions", action="store_true",
                        help="let the local business page confirm or reject link and dependency suggestions")
    parser.add_argument("--decision-model", action="store_true",
                        help="use the decision model set by ONTOLOGYEX_DECIDE_* for link routes, "
                             "review triage, and the command bar (sends binding source, rules, "
                             "and quotes to it)")
    args = parser.parse_args()
    try:
        if args.repo is not None:
            check(args.repo.is_dir() and not args.repo.is_symlink(),
                  "invalid repository root")
        if args.github is not None:
            github_discovery.parse_repository(args.github)
        check(args.snapshot_dir is None or (args.github is not None and not args.state_only),
              "snapshot persistence is for a running --github server only")
        if args.state_only:
            topology = (github_discovery.discover_github(
                args.github, github_discovery.GitHubClient(
                    token=os.environ.get("ONTOLOGYEX_GITHUB_TOKEN")))
                        if args.github else native_view_bridge.native_discover(args.repo)
                        if args.view_engine == "rust" or
                        native_view_bridge.discovery_engine(args.discovery_engine) == "rust"
                        else discover_repo.discover(args.repo))
            if not args.github:
                topology = with_code_surface(topology, args.repo)
            if args.dependencies is not None and args.dependencies.exists():
                topology = dependency_suggest.with_confirmed(
                    topology, dependency_suggest.read_dependency_document(args.dependencies)[0],
                    args.dependencies.name)
            result = build_state(topology, read_observations(args.observations),
                                 read_thresholds(args.thresholds))
            result["discovery_source"] = topology.get("source", {"provider": "local"})
            if args.skill:
                link_rows, rejected = (business_view.read_link_document(args.links)
                                       if args.links is not None else ([], []))
                result["business"] = business_view.project(args.skill, result, link_rows, args.events)
                result["business"]["link_suggestions"] = link_suggest.suggest(
                    args.skill, result, link_rows, rejected)
            print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        else:
            serve(args.repo, args.observations, args.thresholds, args.port,
                  github=args.github, github_refresh_seconds=args.github_refresh_seconds,
                  snapshot_dir=args.snapshot_dir, view_engine=args.view_engine,
                  discovery_engine=args.discovery_engine,
                  skills=args.skill, links=args.links, events=args.events,
                  allow_link_decisions=args.allow_link_decisions,
                  decision_model=args.decision_model, dependencies=args.dependencies,
                  print_bundle=args.bundle_only, proposals=args.proposals, rules_file=args.rules,
                  history=args.history, history_seconds=args.history_seconds, operations_dir=args.operations,
                  live_pulls=args.pulls)
        return 0
    except (OSError, RoomError, discover_repo.DiscoveryError,
            github_discovery.GitHubDiscoveryError, snapshot_store.SnapshotError,
            native_view_bridge.NativeViewError, DomainError, decide.DecisionError, ValueError,
            TypeError, RecursionError) as exc:
        print(json.dumps({"status": "ERROR", "reason": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
