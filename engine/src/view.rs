//! Bounded, read-only view projection for OntologyEX's existing Python graph state.
//!
//! This is a Rust migration kernel, not a deployed server or authenticated
//! collector. It preserves source evidence and never infers runtime causality.
//! The Python endpoint remains the default until cross-language parity gates pass.

use serde_json::{json, Value};
use std::collections::{BTreeMap, BTreeSet, VecDeque};

const MAX_NODES: usize = 10_000;
const MAX_EDGES: usize = 40_000;
const MAX_PAGE: usize = 90;
const MAX_PAGE_EDGES: usize = 300;
const MAX_NODE_EDGES: usize = 40;
const MAX_OFFSET: usize = 10_000;
const MAX_QUERY: usize = 90;

fn error(message: &str) -> String {
    message.to_owned()
}
fn array<'a>(value: &'a Value, field: &str) -> Result<&'a Vec<Value>, String> {
    value.get(field).and_then(Value::as_array)
        .ok_or_else(|| format!("missing or invalid {field}"))
}
fn string<'a>(value: &'a Value, field: &str) -> Result<&'a str, String> {
    value.get(field).and_then(Value::as_str)
        .ok_or_else(|| format!("missing or invalid {field}"))
}
fn field(full: &Value, field: &str) -> Result<Value, String> {
    full.get(field).cloned().ok_or_else(|| format!("missing {field}"))
}
fn optional(full: &Value, field: &str, fallback: Value) -> Value {
    full.get(field).cloned().unwrap_or(fallback)
}
fn valid_id(s: &str) -> bool {
    !s.is_empty() && s.len() <= 1024 && !s.chars().any(char::is_control)
}
fn valid_query(query: &str) -> bool {
    query.len() <= MAX_QUERY && query.bytes().all(|b| (32..=126).contains(&b))
}
fn severity(n: &Value) -> u8 {
    match n.get("status").and_then(Value::as_str).unwrap_or("") {
        "error" => 0, "warning" => 1, "stale" => 2,
        "unknown" => 3, "ok" => 4, _ => 5,
    }
}
fn priority(n: &Value) -> (u8, u8, String, String) {
    let impacts = n.get("potentially_impacted_by").and_then(Value::as_array)
        .map_or(false, |rows| !rows.is_empty());
    (severity(n), if impacts { 0 } else { 1 },
     n.get("label").and_then(Value::as_str).unwrap_or("").to_lowercase(),
     n.get("id").and_then(Value::as_str).unwrap_or("").to_owned())
}
fn relation_rank(relation: &str) -> u8 {
    match relation {
        "DEPENDS_ON" => 0, "STATIC_IMPORTS" => 1, "CALLS" => 2,
        "READS_FROM" => 3, "WRITES_TO" => 4,
        "DECLARES_TABLE_CANDIDATE" => 5, "DECLARES" => 6, _ => 7,
    }
}
fn edge_rank(e: &Value) -> (u8, String, String, String, i64, String) {
    let relation = e.get("relation").and_then(Value::as_str).unwrap_or("");
    (relation_rank(relation), relation.to_owned(),
     e.get("source").and_then(Value::as_str).unwrap_or("").to_owned(),
     e.get("target").and_then(Value::as_str).unwrap_or("").to_owned(),
     e.get("source_line").and_then(Value::as_i64).unwrap_or(0),
     e.get("evidence_id").and_then(Value::as_str).unwrap_or("").to_owned())
}
fn page_edge_rank(e: &Value) -> (String, String, String, i64) {
    (e.get("source").and_then(Value::as_str).unwrap_or("").to_owned(),
     e.get("target").and_then(Value::as_str).unwrap_or("").to_owned(),
     e.get("relation").and_then(Value::as_str).unwrap_or("").to_owned(),
     e.get("source_line").and_then(Value::as_i64).unwrap_or(0))
}
fn lens_accepts(n: &Value, lens: &str) -> bool {
    let kind = n.get("kind").and_then(Value::as_str).unwrap_or("");
    match lens {
        "all" => true,
        "api" => matches!(kind, "project" | "api_route" | "source_module"),
        "database" => matches!(kind, "project" | "declared_db_table" | "source_module"),
        "services" => matches!(kind, "project" | "declared_service"),
        _ => false,
    }
}
fn source_evidence(full: &Value, keys: &BTreeSet<String>) -> Result<Vec<Value>, String> {
    Ok(array(full, "source_evidence")?.iter().filter(|e|
        e.get("id").and_then(Value::as_str).map_or(false, |id| keys.contains(id))
    ).cloned().collect())
}
fn evidence_keys(nodes: &[Value], edges: &[Value]) -> BTreeSet<String> {
    nodes.iter().chain(edges.iter()).filter_map(|x|
        x.get("evidence_id").and_then(Value::as_str).map(str::to_owned)
    ).collect()
}

/// Minimal conservative validation of a source-backed local state.
/// The input must never grant authority for actions, even accidentally.
pub fn validate_source(full: &Value) -> Result<(), String> {
    if full.get("execution_authorized") != Some(&Value::Bool(false)) {
        return Err(error("source graph must not authorize execution"));
    }
    if string(full, "kind")? != "LOCAL_CONTROL_ROOM_STATE" {
        return Err(error("expected local source-backed control room state"));
    }
    let nodes = array(full, "nodes")?;
    let edges = array(full, "edges")?;
    if nodes.len() > MAX_NODES || edges.len() > MAX_EDGES {
        return Err(error("graph exceeds strict count budget"));
    }
    let mut ids = BTreeSet::new();
    for n in nodes {
        let id = string(n, "id")?;
        if !valid_id(id) || !ids.insert(id) || string(n, "label")?.len() > 1024 {
            return Err(error("invalid or duplicate source node"));
        }
    }
    for e in edges {
        if !ids.contains(string(e, "source")?) || !ids.contains(string(e, "target")?) ||
            !valid_id(string(e, "evidence_id")?) {
            return Err(error("dangling or invalid source edge"));
        }
    }
    for field_name in ["coverage", "project", "mode", "generated_at",
                       "runtime_discovery", "impact_semantics"] {
        if full.get(field_name).is_none() {
            return Err(format!("missing {field_name}"));
        }
    }
    Ok(())
}

/// Compact dashboard metadata; the graph itself never appears here.
pub fn summary(full: &Value) -> Result<Value, String> {
    validate_source(full)?;
    let nodes = array(full, "nodes")?;
    let edges = array(full, "edges")?;
    let gaps = array(full, "gaps")?;
    let unresolved = array(full, "unresolved")?;
    let mut active: Vec<&Value> = nodes.iter().filter(|n|
        matches!(n.get("status").and_then(Value::as_str), Some("error" | "warning")) ||
        n.get("potentially_impacted_by").and_then(Value::as_array)
          .map_or(false, |a| !a.is_empty())
    ).collect();
    active.sort_by_key(|n| priority(n));
    let alerts: Vec<Value> = active.iter().take(18).map(|n| json!({
        "id": n.get("id"), "label": n.get("label"), "status": n.get("status"),
        "possible_impact": n.get("potentially_impacted_by")
          .and_then(Value::as_array).map_or(false, |a| !a.is_empty())
    })).collect();
    Ok(json!({
        "schema_version": 1, "kind": "LOCAL_CONTROL_ROOM_SUMMARY",
        "mode": field(full,"mode")?, "generated_at": field(full,"generated_at")?,
        "project": field(full,"project")?, "coverage": field(full,"coverage")?,
        "total_nodes": nodes.len(), "total_edges": edges.len(), "alerts": alerts,
        "total_alerts": active.len(),
        "gaps": gaps.iter().take(24).cloned().collect::<Vec<_>>(),
        "total_gaps": gaps.len(), "unresolved_count": unresolved.len(),
        "discovery_source": optional(full,"discovery_source",json!({"provider":"local"})),
        "discovery_freshness": optional(full,"discovery_freshness",json!("UNKNOWN")),
        "discovery_scanned_at": optional(full,"discovery_scanned_at",Value::Null),
        "durability": optional(full,"durability",json!("MEMORY_ONLY")),
        "snapshot_history": optional(full,"snapshot_history",json!([]))
          .as_array().ok_or_else(|| error("invalid snapshot history"))?
          .iter().take(8).cloned().collect::<Vec<_>>(),
        "runtime_discovery": field(full,"runtime_discovery")?,
        "execution_authorized": false
    }))
}

/// Search and paginate server-side; keep only edges between visible endpoints.
pub fn page(full: &Value, lens: &str, query: &str, offset: usize) -> Result<Value, String> {
    validate_source(full)?;
    if !matches!(lens, "all" | "api" | "database" | "services") ||
       !valid_query(query) || offset > MAX_OFFSET {
        return Err(error("invalid page query"));
    }
    let q = query.to_ascii_lowercase().trim().to_owned();
    let mut matches: Vec<&Value> = array(full, "nodes")?.iter().filter(|n|
        lens_accepts(n,lens) && (q.is_empty() ||
          n.get("id").and_then(Value::as_str).map_or(false, |s| s.to_lowercase().contains(&q)) ||
          n.get("label").and_then(Value::as_str).map_or(false, |s| s.to_lowercase().contains(&q)))
    ).collect();
    matches.sort_by_key(|n| priority(n));
    let total = matches.len();
    let selected: Vec<Value> = matches.iter().skip(offset).take(MAX_PAGE)
        .map(|n| (*n).clone()).collect();
    let ids: BTreeSet<&str> = selected.iter().filter_map(|n| n.get("id").and_then(Value::as_str))
        .collect();
    let mut related: Vec<Value> = array(full, "edges")?.iter().filter(|e|
        e.get("source").and_then(Value::as_str).map_or(false, |s| ids.contains(s)) &&
        e.get("target").and_then(Value::as_str).map_or(false, |s| ids.contains(s))
    ).cloned().collect();
    related.sort_by_key(page_edge_rank);
    let count = related.len();
    related.truncate(MAX_PAGE_EDGES);
    let evidence = source_evidence(full, &evidence_keys(&selected,&related))?;
    let next = offset.saturating_add(selected.len());
    Ok(json!({
        "schema_version":1, "kind":"BOUNDED_GRAPH_PAGE",
        "summary":summary(full)?, "lens":lens,"query":query,"offset":offset,
        "nodes":selected, "edges":related,"source_evidence":evidence,
        "visible_count":selected.len(), "total_matches":total,
        "total_nodes":array(full,"nodes")?.len(),
        "hidden_matches":total.saturating_sub(selected.len()),
        "next_offset":if next<total && next<=MAX_OFFSET { Some(next) } else { None },
        "edge_count_in_page":count, "edges_omitted":count.saturating_sub(MAX_PAGE_EDGES),
        "coverage_semantics":"SERVER_SIDE_SOURCE_GRAPH_SLICE_NOT_FULL_BUSINESS",
        "runtime_discovery":field(full,"runtime_discovery")?,
        "execution_authorized":false
    }))
}

/// Bounded, deterministic 1/2-hop navigation without reversing edge assertions.
pub fn neighborhood(full: &Value, center: &str, depth: usize) -> Result<Value, String> {
    validate_source(full)?;
    if !valid_id(center) || !(1..=2).contains(&depth) {
        return Err(error("invalid neighborhood query"));
    }
    let nodes = array(full,"nodes")?;
    let edges = array(full,"edges")?;
    let by_id: BTreeMap<&str,&Value> = nodes.iter()
        .filter_map(|n| n.get("id").and_then(Value::as_str).map(|id|(id,n))).collect();
    if !by_id.contains_key(center) {
        return Err(error("unknown graph center"));
    }
    let project_id = string(full,"project")?;
    let mut adjacency: BTreeMap<&str,Vec<(&str,usize)>> = BTreeMap::new();
    for (idx,e) in edges.iter().enumerate() {
        let source = string(e,"source")?;
        let target = string(e,"target")?;
        adjacency.entry(source).or_default().push((target,idx));
        if source != target {adjacency.entry(target).or_default().push((source,idx));}
    }
    let mut visited = BTreeSet::from([center]);
    let mut queue = VecDeque::from([(center,0usize)]);
    let mut clipped: BTreeSet<&str> = BTreeSet::new();
    let mut explored=0usize;
    let mut max_depth=0usize;
    while let Some((current,dist))=queue.pop_front() {
        if dist>=depth || (current==project_id && current!=center) {continue;}
        explored+=1;
        let mut candidates=adjacency.get(current).cloned().unwrap_or_default();
        candidates.sort_by(|a,b| {
            let sa=severity(by_id[a.0]);
            let sb=severity(by_id[b.0]);
            sa.cmp(&sb).then_with(||edge_rank(&edges[a.1]).cmp(&edge_rank(&edges[b.1])))
              .then_with(||a.0.cmp(b.0))
        });
        for (neighbor,_) in candidates {
            if visited.contains(neighbor) {continue;}
            if visited.len()>=MAX_PAGE {
                clipped.insert(neighbor);continue;
            }
            visited.insert(neighbor);
            queue.push_back((neighbor,dist+1));
            max_depth=max_depth.max(dist+1);
        }
    }
    let mut view_nodes:Vec<Value> = Vec::new();
    view_nodes.push((*by_id[center]).clone());
    let mut other:Vec<&Value> = visited.iter().filter(|id|**id!=center)
        .map(|id|by_id[id]).collect();
    other.sort_by_key(|n|priority(n));
    view_nodes.extend(other.into_iter().cloned());
    let mut view_edges:Vec<Value> = edges.iter().filter(|e|
        e.get("source").and_then(Value::as_str).map_or(false, |s| visited.contains(s)) &&
        e.get("target").and_then(Value::as_str).map_or(false, |s| visited.contains(s))
    ).cloned().collect();
    view_edges.sort_by_key(edge_rank);
    let internal_count=view_edges.len();
    view_edges.truncate(MAX_PAGE_EDGES);
    let evidence=source_evidence(full,&evidence_keys(&view_nodes,&view_edges))?;
    let incoming=edges.iter().filter(|e|e.get("target").and_then(Value::as_str)==Some(center)).count();
    let outgoing=edges.iter().filter(|e|e.get("source").and_then(Value::as_str)==Some(center)).count();
    Ok(json!({
        "schema_version":1, "kind":"BOUNDED_GRAPH_NEIGHBORHOOD",
        "summary":summary(full)?, "center_id":center,
        "requested_depth":depth,"depth_reached":max_depth,
        "nodes":view_nodes,"edges":view_edges,"source_evidence":evidence,
        "visible_count":view_nodes.len(),"total_matches":view_nodes.len(),
        "total_nodes":nodes.len(),"offset":0,"next_offset":Value::Null,
        "direct_incoming_edges":incoming,"direct_outgoing_edges":outgoing,
        "explored_nodes":explored,"truncated_node_candidates":clipped.len(),
        "edge_count_in_view":internal_count,
        "edges_omitted":internal_count.saturating_sub(MAX_PAGE_EDGES),
        "is_complete_within_depth":clipped.is_empty() && internal_count<=MAX_PAGE_EDGES,
        "scope":"BIDIRECTIONAL_GRAPH_NAVIGATION_NOT_CAUSAL_IMPACT",
        "runtime_discovery":field(full,"runtime_discovery")?,
        "execution_authorized":false
    }))
}

/// One source-backed node with bounded adjacent edges and relevant evidence.
pub fn node_detail(full:&Value, id:&str)->Result<Value,String>{
    validate_source(full)?;
    if !valid_id(id) {return Err(error("invalid node identity"));}
    let node=array(full,"nodes")?.iter().find(|n|
        n.get("id").and_then(Value::as_str)==Some(id))
        .ok_or_else(||error("node not found"))?;
    let mut edges:Vec<Value> = array(full,"edges")?.iter().filter(|e|
        e.get("source").and_then(Value::as_str)==Some(id) ||
        e.get("target").and_then(Value::as_str)==Some(id)).cloned().collect();
    edges.sort_by_key(|e|{
        (e.get("relation").and_then(Value::as_str).unwrap_or("").to_owned(),
         e.get("source").and_then(Value::as_str).unwrap_or("").to_owned(),
         e.get("target").and_then(Value::as_str).unwrap_or("").to_owned(),
         e.get("source_line").and_then(Value::as_i64).unwrap_or(0))
    });
    let total=edges.len();
    edges.truncate(MAX_NODE_EDGES);
    let evidence=source_evidence(full,&evidence_keys(&[node.clone()],&edges))?;
    Ok(json!({
        "schema_version":1,"kind":"BOUNDED_NODE_DETAIL",
        "node":node,"edges":edges,"total_related_edges":total,
        "edges_omitted":total.saturating_sub(MAX_NODE_EDGES),
        "source_evidence":evidence,
        "impact_semantics":field(full,"impact_semantics")?,
        "execution_authorized":false
    }))
}

#[cfg(test)]
mod tests {
    use super::*;
    fn fixture() -> Value {
        json!({
            "kind":"LOCAL_CONTROL_ROOM_STATE","execution_authorized":false,
            "mode":"SOURCE_REPORTED_NOT_INDEPENDENTLY_ATTESTED",
            "generated_at":"2026-10-27T21:00:00Z",
            "project":"project","impact_semantics":"NOT_CAUSAL",
            "runtime_discovery":"NOT_IMPLEMENTED",
            "coverage":{"node_status_counts":{"unknown":4}},
            "unresolved":[],"gaps":[],"source_evidence":[{"id":"e1","path":"compose.yml"}],
            "nodes":[
              {"id":"project","kind":"project","label":"Project","status":"unknown","evidence_id":"e1","potentially_impacted_by":[]},
              {"id":"web","kind":"declared_service","label":"web","status":"unknown","evidence_id":"e1","potentially_impacted_by":[]},
              {"id":"api","kind":"declared_service","label":"api","status":"unknown","evidence_id":"e1","potentially_impacted_by":[]},
              {"id":"db","kind":"declared_service","label":"db","status":"unknown","evidence_id":"e1","potentially_impacted_by":[]}
            ],
            "edges":[
              {"source":"project","target":"web","relation":"DECLARES","evidence_id":"e1"},
              {"source":"project","target":"api","relation":"DECLARES","evidence_id":"e1"},
              {"source":"project","target":"db","relation":"DECLARES","evidence_id":"e1"},
              {"source":"web","target":"api","relation":"DEPENDS_ON","evidence_id":"e1"},
              {"source":"api","target":"db","relation":"DEPENDS_ON","evidence_id":"e1"}
            ]
        })
    }
    #[test]
    fn summary_excludes_entire_graph_and_remains_unattested(){
        let s=summary(&fixture()).unwrap();
        assert!(s.get("nodes").is_none());
        assert_eq!(s["total_nodes"],4);
        assert_eq!(s["execution_authorized"],false);
    }
    #[test]
    fn page_is_bounded_and_searchable(){
        let p=page(&fixture(),"services","web",0).unwrap();
        assert_eq!(p["total_matches"],1);
        assert_eq!(p["nodes"][0]["id"],"web");
        assert!(p["edges"].as_array().unwrap().is_empty());
    }
    #[test]
    fn directed_edges_survive_two_hop_navigation(){
        let n=neighborhood(&fixture(),"web",2).unwrap();
        let edges=n["edges"].as_array().unwrap();
        assert!(edges.iter().any(|e|e["source"]=="web"&&e["target"]=="api"));
        assert!(edges.iter().any(|e|e["source"]=="api"&&e["target"]=="db"));
        assert_eq!(n["execution_authorized"],false);
    }
    #[test]
    fn strict_invalid_input_and_action_grant_rejected(){
        assert!(page(&fixture(),"invalid","",0).is_err());
        assert!(neighborhood(&fixture(),"missing",1).is_err());
        let mut state=fixture();state["execution_authorized"]=json!(true);
        assert!(summary(&state).is_err());
    }
    #[test]
    fn adjacent_evidence_is_retained(){
        let d=node_detail(&fixture(),"api").unwrap();
        assert_eq!(d["node"]["id"],"api");
        assert_eq!(d["source_evidence"][0]["id"],"e1");
    }
}
