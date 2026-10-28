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
from threading import Event, RLock, Thread
import json
import math
import os
import re
import stat

import discover_repo
import github_discovery
import snapshot_store
import graph_view_api
import incident_view
from urllib.parse import parse_qs, urlsplit

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
            "source_line": node.get("source_line"),
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
<h1>BUSINESS CONTROL ROOM</h1></div><div><select id="lens" aria-label="Graph lens">
<option value="all">ALL SYSTEMS</option><option value="api">API ROUTES</option>
<option value="database">DATABASE</option><option value="services">SERVICES</option>
</select> <input id="graph-search" type="search" aria-label="Find graph node" maxlength="90" placeholder="Find node…"/>
<label for="graph-share" title="Resize the graph area">Graph size</label>
<input id="graph-share" type="range" min="48" max="76" value="63" aria-label="Graph area width"/>
<button id="graph-focus" type="button" aria-pressed="false">Focus graph</button> <button id="map-business" type="button">Map My Business</button><div id="mode" class="muted">Connecting to local source...</div><small id="updated"></small></div></header>
<main id="workspace" aria-label="Adaptive business workspace">
<section id="map" aria-label="Declared component topology"><p id="graph-coverage" aria-live="polite"></p><div id="graph-legend" aria-label="Graph edge legend">
<span>Amber arrows: declared dependency. Teal dashed arrows: other source relationships. Direction is source → target, not runtime causation.</span></div>
<div id="graph-pager">
<button id="graph-back" type="button" disabled>Back to list</button>
<label for="graph-depth">Hops</label>
<select id="graph-depth" aria-label="Neighborhood depth"><option value="1">1 hop</option><option value="2">2 hops</option></select>
<button id="graph-prev" type="button" disabled>Previous</button>
<button id="graph-next" type="button" disabled>Next</button>
</div><p id="graph-scope" aria-live="polite">Select a node to explore its connected neighbors.</p><svg id="svg" role="img" aria-label="Interactive dependency diagram"></svg></section>
<section id="incidents" class="priority-critical"><h2>Incidents and risks</h2><ul id="incident-list"></ul></section>
<section id="integrity" class="priority-critical"><h2>System integrity</h2><div id="counts"></div></section>
<section id="details-panel"><h2>Selected component</h2><div id="details">Select a node to inspect its source and status.</div><button type="button" id="investigate-node" disabled>Investigate evidence</button><pre id="incident-report" aria-live="polite">Select a node to inspect its observations and possible exposures.</pre></section>
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
 for(const [id,fill] of [["dependency-arrow","#e5b66f"],["source-arrow","#2e8297"]]){
   const marker=el("marker",{id,viewBox:"0 0 10 10",refX:9,refY:5,
     markerWidth:6,markerHeight:6,orient:"auto",markerUnits:"strokeWidth"});
   marker.append(el("path",{d:"M 0 0 L 10 5 L 0 10 z",fill}));
   defs.append(marker);
 }
 svg.append(defs);
 for(const e of s.edges){const a=pos[e.source],b=pos[e.target];if(!a||!b)continue;
 const x1=a.x+nodeW,y1=a.y+nodeH/2,x2=b.x,y2=b.y+nodeH/2;
 const d="M"+x1+","+y1+" C"+(x1+38)+","+y1+" "+(x2-38)+","+y2+" "+x2+","+y2;
 const path=el("path",{d,stroke:e.relation==="DEPENDS_ON"?"#e5b66f":"#2e8297",
 "stroke-opacity":.65,"stroke-width":e.relation==="DEPENDS_ON"?2:1.35,
 "stroke-dasharray":e.relation==="DEPENDS_ON"?"":"5 6",
 "marker-end":e.relation==="DEPENDS_ON"?"url(#dependency-arrow)":"url(#source-arrow)",
 fill:"none"});
 path.append(el("title",{},e.relation+" · "+e.source+" → "+e.target+
   (e.source_line?" · source line "+e.source_line:"")+
   " · source relationship, not observed runtime execution"));
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
          github_client=None, snapshot_dir: Path | None = None) -> None:
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

    def snapshot(observation_rows: list[dict] | None = None) -> dict:
        current = datetime.now(timezone.utc)
        if remote is not None:
            topology, last_success, refresh_error = remote.snapshot()
        else:
            with cache_lock:
                last_attempt = state_cache.get("last_attempt")
                if last_attempt is None or (current - last_attempt).total_seconds() > 15:
                    state_cache["last_attempt"] = current
                    topology = discover_repo.discover(repo)
                    state_cache["topology"] = topology
                    state_cache["last_success"] = current
                topology = state_cache["topology"]
                last_success = state_cache["last_success"]
                refresh_error = False
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
                        result = graph_view_api.summary(full)
                    elif route.path == "/api/view":
                        if not set(params).issubset({"lens", "q", "offset"}):
                            raise graph_view_api.ViewError("unsupported view parameter")
                        result = graph_view_api.page(
                            full, lens=params.get("lens", ["all"])[0],
                            query=params.get("q", [""])[0],
                            offset=graph_view_api.parse_offset(params.get("offset", ["0"])[0]))
                    elif route.path == "/api/neighborhood":
                        if set(params) != {"id", "depth"}:
                            raise graph_view_api.ViewError("neighborhood requires id and depth")
                        result = graph_view_api.neighborhood(
                            full, params["id"][0],
                            depth=graph_view_api.valid_depth(params["depth"][0]))
                    elif route.path == "/api/incident":
                        if set(params) != {"id"}:
                            raise graph_view_api.ViewError("incident requires exact node id")
                        rows = read_observations(observations)
                        full = snapshot(rows)  # same rows for status and timeline
                        result = incident_view.incident(full, rows, params["id"][0])
                    else:
                        if set(params) != {"id"}:
                            raise graph_view_api.ViewError("node requires an exact id")
                        result = graph_view_api.node_detail(full, params["id"][0])
                    payload = json.dumps(result, sort_keys=True).encode()
                    code = 200
                except (graph_view_api.ViewError, incident_view.IncidentError, ValueError, UnicodeError):
                    payload, code = b'{"status":"INVALID_QUERY"}', 400
                except (RoomError, discover_repo.DiscoveryError, github_discovery.GitHubDiscoveryError,
                        OSError, TypeError):
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

        def log_message(self, *args):
            return  # No user-supplied paths or request logs persisted.

    # No POST, no write operations and no external listener.
    try:
        with ThreadingHTTPServer(("127.0.0.1", port), Handler) as server:
            print(f"OntologyEX local control room: http://127.0.0.1:{server.server_port}",
                  flush=True)
            server.serve_forever()
    finally:
        stop_worker.set()
        if worker is not None:
            worker.join(timeout=2)


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
                        if args.github else discover_repo.discover(args.repo))
            result = build_state(topology, read_observations(args.observations),
                                 read_thresholds(args.thresholds))
            result["discovery_source"] = topology.get("source", {"provider": "local"})
            print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        else:
            serve(args.repo, args.observations, args.thresholds, args.port,
                  github=args.github, github_refresh_seconds=args.github_refresh_seconds,
                  snapshot_dir=args.snapshot_dir)
        return 0
    except (OSError, RoomError, discover_repo.DiscoveryError,
            github_discovery.GitHubDiscoveryError, snapshot_store.SnapshotError,
            ValueError, TypeError, RecursionError) as exc:
        print(json.dumps({"status": "ERROR", "reason": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
