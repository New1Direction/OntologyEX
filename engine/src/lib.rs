//! OntologyEX foundational graph engine: source-backed topology + time-aware observations.
//!
//! Deliberately conservative. A reported status is *not* independently authenticated
//! health. Possible dependent impacts are *not* proven outages or causal roots.
//! No network, execution, writing, or credentials are used by this library.

pub mod view;
pub mod incident;
pub mod business;
pub mod context;
pub mod decide;
pub mod pyjson;
#[cfg(feature = "discover")]
pub mod pyyaml;
#[cfg(feature = "discover")]
pub mod discover;
#[cfg(feature = "feeds")]
pub mod feeds;
#[cfg(feature = "feeds")]
pub mod feeds_net;

use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet, VecDeque};

pub const MAX_NODES: usize = 10_000;
pub const MAX_EDGES: usize = 40_000;
pub const MAX_OBSERVATIONS: usize = 100_000;
pub const MAX_TTL_MS: u64 = 86_400_000;
pub const MAX_DEPTH: usize = 64;

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Node {
    pub id: String,
    pub kind: String,
    pub label: String,
    pub evidence_id: String,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum EdgeKind {
    DependsOn,
    Calls,
    ReadsFrom,
    WritesTo,
    Contains,
}

impl EdgeKind {
    // Only strict declared operational dependencies propagate hypothetical impact.
    fn is_dependency(self) -> bool {
        matches!(self, Self::DependsOn | Self::Calls | Self::ReadsFrom | Self::WritesTo)
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Edge {
    pub from: String,
    pub to: String,
    pub kind: EdgeKind,
    pub evidence_id: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Topology {
    pub schema_version: u32,
    pub root_goal_id: String,
    pub version_id: String,
    pub nodes: Vec<Node>,
    pub edges: Vec<Edge>,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum ReportedHealth {
    Healthy,
    Degraded,
    Failed,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Observation {
    pub component_id: String,
    pub source_id: String,
    pub observed_at_ms: i64,
    pub ttl_ms: u64,
    pub reported_health: ReportedHealth,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum DisplayHealth {
    Unknown,
    Stale,
    Healthy,
    Degraded,
    Failed,
    Conflicting,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct NodeProjection {
    pub id: String,
    pub label: String,
    pub kind: String,
    pub evidence_id: String,
    pub health: DisplayHealth,
    pub latest_observed_at_ms: Option<i64>,
    pub report_source_ids: Vec<String>,
    pub potentially_impacted_by: Vec<String>,
}

#[derive(Clone, Debug, Serialize)]
pub struct Projection {
    pub schema_version: u32,
    pub root_goal_id: String,
    pub topology_version: String,
    pub evaluated_at_ms: i64,
    pub nodes: Vec<NodeProjection>,
    pub edge_count: usize,
    pub unmapped_observation_count: usize,
    pub future_observation_count: usize,
    pub trust: &'static str,
    pub impact_semantics: &'static str,
    pub execution_authorized: bool,
}

#[derive(Debug, PartialEq, Eq)]
pub enum GraphError {
    Invalid(String),
    TooLarge(String),
}

impl std::fmt::Display for GraphError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Invalid(s) | Self::TooLarge(s) => write!(f, "{s}"),
        }
    }
}

impl std::error::Error for GraphError {}

fn valid_id(s: &str) -> bool {
    !s.is_empty()
        && s.len() <= 200
        && s.bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'-' | b'.' | b':' | b'/'))
}

fn valid_text(s: &str) -> bool {
    !s.is_empty() && s.len() <= 200 && !s.chars().any(char::is_control)
}

pub fn validate(topology: &Topology, observations: &[Observation]) -> Result<(), GraphError> {
    if topology.schema_version != 1
        || !valid_id(&topology.root_goal_id)
        || !valid_id(&topology.version_id)
    {
        return Err(GraphError::Invalid("invalid topology version or identity".into()));
    }
    if topology.nodes.len() > MAX_NODES
        || topology.edges.len() > MAX_EDGES
        || observations.len() > MAX_OBSERVATIONS
    {
        return Err(GraphError::TooLarge("graph/observation count limit".into()));
    }
    let mut ids = BTreeSet::new();
    for n in &topology.nodes {
        if !valid_id(&n.id)
            || !valid_id(&n.evidence_id)
            || !valid_id(&n.kind)
            || !valid_text(&n.label)
            || !ids.insert(&n.id)
        {
            return Err(GraphError::Invalid("invalid or duplicate node".into()));
        }
    }
    let mut edges = BTreeSet::new();
    for e in &topology.edges {
        if !ids.contains(&e.from)
            || !ids.contains(&e.to)
            || !valid_id(&e.evidence_id)
            || !edges.insert((&e.from, &e.to, e.kind as u8))
        {
            return Err(GraphError::Invalid(
                "dangling or duplicate declared graph edge".into(),
            ));
        }
    }
    for o in observations {
        // Unknown component observations remain visible as a count; don't make up nodes.
        if !valid_id(&o.component_id)
            || !valid_id(&o.source_id)
            || o.observed_at_ms < 0
            || o.ttl_ms == 0
            || o.ttl_ms > MAX_TTL_MS
        {
            return Err(GraphError::Invalid("invalid observation metadata".into()));
        }
    }
    Ok(())
}

/// Evaluate as-of one explicit time, so replay is deterministic.
/// One source reporting different health at its newest time produces Conflicting,
/// never Healthy. Different sources combine by severity: the worst fresh report wins.
/// A future-dated sample is ignored and recorded, never used as live evidence.
pub fn project(
    topology: &Topology,
    observations: &[Observation],
    now_ms: i64,
) -> Result<Projection, GraphError> {
    validate(topology, observations)?;
    if now_ms < 0 {
        return Err(GraphError::Invalid("invalid evaluation time".into()));
    }
    let id_set: BTreeSet<&str> = topology.nodes.iter().map(|n| n.id.as_str()).collect();
    let mut events: BTreeMap<&str, Vec<&Observation>> = BTreeMap::new();
    let mut unmapped = 0;
    let mut future = 0;
    for o in observations {
        if !id_set.contains(o.component_id.as_str()) {
            unmapped += 1;
        } else if o.observed_at_ms > now_ms {
            future += 1;
        } else {
            events.entry(o.component_id.as_str()).or_default().push(o);
        }
    }

    let mut output: BTreeMap<&str, NodeProjection> = BTreeMap::new();
    let mut failed: Vec<&str> = Vec::new();
    for n in &topology.nodes {
        let reports = events.get(n.id.as_str());
        let newest = reports.and_then(|rows| rows.iter().map(|o| o.observed_at_ms).max());
        let mut sources = BTreeSet::new();
        let health = match reports {
            None => DisplayHealth::Unknown,
            Some(rows) => {
                // Newest report per source. A source that disagrees with itself at
                // its newest time fails closed; different sources combine by severity.
                let mut per_source: BTreeMap<&str, Vec<&Observation>> = BTreeMap::new();
                for o in rows {
                    per_source.entry(o.source_id.as_str()).or_default().push(o);
                }
                let mut conflicting = false;
                let mut worst: Option<u8> = None;
                for (source, own) in &per_source {
                    let time = own.iter().map(|o| o.observed_at_ms).max().expect("nonempty");
                    let latest: BTreeSet<u8> = own.iter().filter(|o| o.observed_at_ms == time)
                        .map(|o| o.reported_health as u8).collect();
                    if latest.len() > 1 {
                        conflicting = true;
                        continue;
                    }
                    let ttl = own.iter().filter(|o| o.observed_at_ms == time)
                        .map(|o| o.ttl_ms).max().expect("nonempty");
                    if (now_ms - time) as u64 <= ttl {
                        sources.insert((*source).to_owned());
                        let level = *latest.iter().next().expect("one health");
                        worst = Some(worst.map_or(level, |w| w.max(level)));
                    }
                }
                if conflicting {
                    sources.clear();
                    DisplayHealth::Conflicting
                } else {
                    // Reported only: no transport/source authentication in this engine.
                    match worst {
                        None => DisplayHealth::Stale,
                        Some(x) if x == ReportedHealth::Healthy as u8 => DisplayHealth::Healthy,
                        Some(x) if x == ReportedHealth::Degraded as u8 => DisplayHealth::Degraded,
                        Some(_) => DisplayHealth::Failed,
                    }
                }
            }
        };
        if health == DisplayHealth::Failed {
            failed.push(&n.id);
        }
        output.insert(&n.id, NodeProjection {
            id: n.id.clone(),
            label: n.label.clone(),
            kind: n.kind.clone(),
            evidence_id: n.evidence_id.clone(),
            health,
            latest_observed_at_ms: newest,
            report_source_ids: sources.into_iter().collect(),
            potentially_impacted_by: Vec::new(),
        });
    }

    // Consumer -> dependency, traverse reverse links only. Never mutate a
    // dependent's observed health just because something it uses reported failure.
    let mut dependents: BTreeMap<&str, BTreeSet<&str>> = BTreeMap::new();
    for edge in &topology.edges {
        if edge.kind.is_dependency() {
            dependents.entry(&edge.to).or_default().insert(&edge.from);
        }
    }
    for broken in failed {
        let mut seen = BTreeSet::from([broken]);
        let mut queue = VecDeque::from([(broken, 0usize)]);
        while let Some((current, depth)) = queue.pop_front() {
            if depth == MAX_DEPTH {
                continue;
            }
            if let Some(children) = dependents.get(current) {
                for child in children {
                    if *child != broken {
                        if let Some(node) = output.get_mut(child) {
                            node.potentially_impacted_by.push(broken.to_owned());
                        }
                    }
                    if seen.insert(*child) {
                        queue.push_back((*child, depth + 1));
                    }
                }
            }
        }
    }
    for node in output.values_mut() {
        node.potentially_impacted_by.sort();
        node.potentially_impacted_by.dedup();
    }
    Ok(Projection {
        schema_version: 1,
        root_goal_id: topology.root_goal_id.clone(),
        topology_version: topology.version_id.clone(),
        evaluated_at_ms: now_ms,
        nodes: output.into_values().collect(),
        edge_count: topology.edges.len(),
        unmapped_observation_count: unmapped,
        future_observation_count: future,
        trust: "UNAUTHENTICATED_INPUT_NOT_PRODUCTION_HEALTH",
        impact_semantics: "DECLARED_DEPENDENCY_POTENTIAL_IMPACT_NOT_CAUSAL_PROOF",
        execution_authorized: false,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn graph() -> Topology {
        Topology {
            schema_version: 1,
            root_goal_id: "repoing".into(),
            version_id: "sha-1".into(),
            nodes: ["web", "api", "db"]
                .into_iter()
                .map(|id| Node {
                    id: id.into(), kind: "service".into(),
                    label: id.into(), evidence_id: format!("E-{id}"),
                })
                .collect(),
            edges: vec![
                Edge { from: "web".into(), to: "api".into(), kind: EdgeKind::DependsOn,
                       evidence_id: "E-1".into() },
                Edge { from: "api".into(), to: "db".into(), kind: EdgeKind::DependsOn,
                       evidence_id: "E-2".into() },
            ],
        }
    }

    fn event(id: &str, health: ReportedHealth, t: i64, ttl: u64) -> Observation {
        Observation {
            component_id: id.into(), source_id: "collector-1".into(),
            observed_at_ms: t, ttl_ms: ttl, reported_health: health,
        }
    }

    fn get<'a>(p: &'a Projection, id: &str) -> &'a NodeProjection {
        p.nodes.iter().find(|n| n.id == id).unwrap()
    }

    #[test]
    fn unobserved_never_healthy() {
        let p = project(&graph(), &[], 200).unwrap();
        assert!(p.nodes.iter().all(|n| n.health == DisplayHealth::Unknown));
        assert!(!p.execution_authorized);
    }
    #[test]
    fn fresh_failure_exposes_possible_dependents_without_inventing_outage() {
        let p = project(&graph(), &[event("db", ReportedHealth::Failed, 100, 101)], 200).unwrap();
        assert_eq!(get(&p, "db").health, DisplayHealth::Failed);
        assert_eq!(get(&p, "api").health, DisplayHealth::Unknown);
        assert_eq!(get(&p, "web").potentially_impacted_by, vec!["db"]);
        assert_eq!(get(&p, "api").potentially_impacted_by, vec!["db"]);
    }
    #[test]
    fn stale_failure_does_not_propagate_as_active() {
        let p = project(&graph(), &[event("db", ReportedHealth::Failed, 100, 50)], 200).unwrap();
        assert_eq!(get(&p, "db").health, DisplayHealth::Stale);
        assert!(get(&p, "api").potentially_impacted_by.is_empty());
    }
    #[test]
    fn future_sample_is_ignored() {
        let p = project(&graph(), &[event("db", ReportedHealth::Healthy, 300, 50)], 200).unwrap();
        assert_eq!(get(&p, "db").health, DisplayHealth::Unknown);
        assert_eq!(p.future_observation_count, 1);
    }
    #[test]
    fn conflicting_fresh_claims_never_become_healthy() {
        let rows = [
            event("db", ReportedHealth::Healthy, 100, 500),
            event("db", ReportedHealth::Failed, 100, 500),
        ];
        let p = project(&graph(), &rows, 200).unwrap();
        assert_eq!(get(&p, "db").health, DisplayHealth::Conflicting);
        assert!(get(&p, "api").potentially_impacted_by.is_empty());
    }
    fn from(source: &str, health: ReportedHealth, t: i64, ttl: u64) -> Observation {
        Observation { source_id: source.into(), ..event("db", health, t, ttl) }
    }
    #[test]
    fn sources_combine_by_severity() {
        let rows = [from("otel", ReportedHealth::Failed, 100, 500),
                    from("probe", ReportedHealth::Healthy, 190, 500)];
        let p = project(&graph(), &rows, 200).unwrap();
        assert_eq!(get(&p, "db").health, DisplayHealth::Failed);
        assert_eq!(get(&p, "db").report_source_ids, vec!["otel", "probe"]);
        assert_eq!(get(&p, "api").potentially_impacted_by, vec!["db"]);
    }
    #[test]
    fn each_source_expires_on_its_own() {
        let rows = [from("otel", ReportedHealth::Failed, 100, 10),
                    from("probe", ReportedHealth::Healthy, 190, 500)];
        let p = project(&graph(), &rows, 200).unwrap();
        assert_eq!(get(&p, "db").health, DisplayHealth::Healthy);
        assert_eq!(get(&p, "db").report_source_ids, vec!["probe"]);
    }
    #[test]
    fn order_independent_and_latest_event_wins() {
        let a = event("db", ReportedHealth::Failed, 100, 500);
        let b = event("db", ReportedHealth::Healthy, 190, 500);
        let x = project(&graph(), &[a.clone(), b.clone()], 200).unwrap();
        let y = project(&graph(), &[b, a], 200).unwrap();
        assert_eq!(serde_json::to_string(&x).unwrap(), serde_json::to_string(&y).unwrap());
        assert_eq!(get(&x, "db").health, DisplayHealth::Healthy);
    }
    #[test]
    fn rejects_dangling_and_duplicate_edges() {
        let mut g = graph();
        g.edges.push(g.edges[0].clone());
        assert!(validate(&g, &[]).is_err());
        g.edges[2].to = "missing".into();
        assert!(validate(&g, &[]).is_err());
    }
    #[test]
    fn cycles_are_bounded_and_do_not_self_implicate() {
        let mut g = graph();
        g.edges.push(Edge { from: "db".into(), to: "web".into(),
                            kind: EdgeKind::DependsOn, evidence_id: "E-cycle".into() });
        let p = project(&g, &[event("db", ReportedHealth::Failed, 150, 100)], 200).unwrap();
        assert!(!get(&p, "db").potentially_impacted_by.contains(&"db".into()));
        assert_eq!(get(&p, "web").potentially_impacted_by, vec!["db"]);
    }
    #[test]
    fn unmapped_sample_does_not_fabricate_node() {
        let p = project(&graph(), &[event("ghost", ReportedHealth::Failed, 150, 100)], 200).unwrap();
        assert_eq!(p.nodes.len(), 3);
        assert_eq!(p.unmapped_observation_count, 1);
    }
    #[test]
    fn invalid_ttl_and_ids_rejected() {
        assert!(project(&graph(), &[event("db", ReportedHealth::Healthy, 100, 0)], 200).is_err());
        let mut g = graph();
        g.nodes[0].id = "BAD\nID".into();
        assert!(validate(&g, &[]).is_err());
    }
    #[test]
    fn contains_is_not_runtime_dependency() {
        let mut g = graph();
        g.edges[0].kind = EdgeKind::Contains;
        let p = project(&g, &[event("db", ReportedHealth::Failed, 150, 100)], 200).unwrap();
        assert!(get(&p, "web").potentially_impacted_by.is_empty());
        assert_eq!(get(&p, "api").potentially_impacted_by, vec!["db"]);
    }
}
