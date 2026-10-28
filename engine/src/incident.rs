//! Deterministic source-reported incident projection; never causal proof.
 //! Caller may supply strict, parsed observation rows, but every row is still
//! **unattested**. No network, file, subprocess or repair authority here.
use chrono::DateTime;
use serde_json::{json, Value};
use std::collections::BTreeSet;

const MAX_EVENTS: usize = 32;
const MAX_DEPENDENTS: usize = 40;
const MAX_REVISIONS: usize = 8;
const MAX_RELATED: usize = 40;
const MAX_OBSERVATIONS: usize = 4096;

fn array<'a>(v: &'a Value, key: &str) -> Result<&'a Vec<Value>, String> {
    v.get(key).and_then(Value::as_array)
        .ok_or_else(|| format!("missing or invalid {key}"))
}
fn string<'a>(v: &'a Value, key: &str) -> Result<&'a str, String> {
    v.get(key).and_then(Value::as_str)
        .ok_or_else(|| format!("missing or invalid {key}"))
}
fn timestamp(v: &str) -> Result<DateTime<chrono::FixedOffset>, String> {
    DateTime::parse_from_rfc3339(v)
        .map_err(|_| "invalid RFC3339 incident observation time".to_owned())
}
fn valid_id(id: &str) -> bool {
    !id.is_empty() && id.len() <= 1024 && !id.chars().any(char::is_control)
}
fn line(edge: &Value) -> i64 {
    edge.get("source_line").and_then(Value::as_i64).unwrap_or(0)
}
fn str_or_empty<'a>(v: &'a Value, key: &str) -> &'a str {
    v.get(key).and_then(Value::as_str).unwrap_or("")
}

/// Match Python's evidence-first incident record on a fixed verified state.
/// No relation proves an actual runtime call, outage, or deployment.
pub fn project(full: &Value, rows: &[Value], component_id: &str) -> Result<Value, String> {
    crate::view::validate_source(full)?;
    if !valid_id(component_id) {
        return Err("invalid incident component identity".into());
    }
    if rows.len() > MAX_OBSERVATIONS {
        return Err("incident observation count exceeds budget".into());
    }
    let nodes = array(full, "nodes")?;
    let current = nodes.iter().find(|n| n.get("id").and_then(Value::as_str)
        == Some(component_id)).ok_or_else(|| "unknown component".to_owned())?;
    let now = timestamp(string(full, "generated_at")?)?;
    let mut timeline = Vec::new();
    for row in rows {
        if row.get("schema_version") != Some(&json!(1)) ||
            !matches!(row.get("status").and_then(Value::as_str), Some("ok"|"warning"|"error")) ||
            !matches!(row.get("source").and_then(Value::as_str),
                      Some("otel"|"health_probe"|"manual_export"|"synthetic")) {
            return Err("invalid incident observation schema/status/source".into());
        }
        let id = string(row, "component_id")?;
        if !valid_id(id) { return Err("invalid incident observation component ID".into()); }
        let ttl = row.get("expires_after_seconds").and_then(Value::as_u64)
            .ok_or_else(|| "invalid incident TTL".to_owned())?;
        if !(1..=86_400).contains(&ttl) {
            return Err("incident TTL out of range".into());
        }
        let metrics = row.get("metrics").and_then(Value::as_object)
            .ok_or_else(|| "missing incident metrics".to_owned())?;
        if metrics.len() > 5 || !metrics.iter().all(|(k, v)| {
            matches!(k.as_str(), "queue_lag_seconds" | "db_pool_utilization_pct" |
                "error_rate_pct" | "p95_latency_ms" | "job_lag_seconds")
                && v.as_f64().is_some_and(|n| n.is_finite() && (0.0..=1e9).contains(&n))
        }) {
            return Err("invalid incident metrics".into());
        }
        let observed = string(row, "observed_at")?;
        let when = timestamp(observed)?;
        if id != component_id || when > now { continue; }
        let age_microseconds = (now - when).num_microseconds()
            .ok_or_else(|| "incident observation age out of range".to_owned())?;
        let age_secs = age_microseconds as f64 / 1_000_000.0;
        let rounded = (age_secs * 1000.0).round() / 1000.0;
        timeline.push(json!({
            "observed_at": observed,
            "status": string(row, "status")?,
            "source": string(row, "source")?,
            "metrics": Value::Object(metrics.clone()),
            "expires_after_seconds": ttl,
            "age_seconds": rounded,
            "fresh_at_evaluation": age_secs <= ttl as f64,
            "authenticity": "UNATTESTED_LOCAL_INPUT"
        }));
    }
    timeline.sort_by(|a,b| {
        (str_or_empty(a,"observed_at"), str_or_empty(a,"source"),
         str_or_empty(a,"status"), a.get("metrics").map(Value::to_string).unwrap_or_default())
        .cmp(&(str_or_empty(b,"observed_at"), str_or_empty(b,"source"),
               str_or_empty(b,"status"), b.get("metrics").map(Value::to_string).unwrap_or_default()))
    });
    let observed_count = timeline.len();
    let timeline: Vec<Value> = timeline.into_iter().rev().take(MAX_EVENTS).collect::<Vec<_>>()
        .into_iter().rev().collect();

    let mut exposure: Vec<&Value> = nodes.iter().filter(|n|
        n.get("potentially_impacted_by").and_then(Value::as_array)
            .is_some_and(|rows| rows.iter().any(|v| v.as_str() == Some(component_id)))
    ).collect();
    exposure.sort_by_key(|n| (
        n.get("status").and_then(Value::as_str) != Some("error"),
        str_or_empty(n,"label").to_owned(), str_or_empty(n,"id").to_owned()
    ));
    let possible_dependents: Vec<Value> = exposure.iter().take(MAX_DEPENDENTS).map(|n|
        json!({"id":n.get("id"),"label":n.get("label"),
               "independently_reported_status":n.get("status")})
    ).collect();

    let mut direct: Vec<&Value> = array(full,"edges")?.iter().filter(|e| {
        e.get("source").and_then(Value::as_str)==Some(component_id)
            || e.get("target").and_then(Value::as_str)==Some(component_id)
    }).collect();
    direct.sort_by_key(|e| (
        str_or_empty(e,"relation").to_owned(),str_or_empty(e,"source").to_owned(),
        str_or_empty(e,"target").to_owned(), line(e)
    ));
    let direct_relationships: Vec<Value> = direct.iter().take(MAX_RELATED)
        .map(|e|(*e).clone()).collect();
    let mut ids = BTreeSet::new();
    if let Some(id) = current.get("evidence_id").and_then(Value::as_str) {
        ids.insert(id);
    }
    for edge in &direct_relationships {
        if let Some(id) = edge.get("evidence_id").and_then(Value::as_str) { ids.insert(id); }
    }
    let source_evidence: Vec<Value> = array(full, "source_evidence")?.iter().filter(|v| {
        v.get("id").and_then(Value::as_str).is_some_and(|id| ids.contains(id))
    }).cloned().collect();
    let history: Vec<Value> = full.get("snapshot_history").and_then(Value::as_array)
        .map(|xs| xs.iter().take(MAX_REVISIONS).cloned().collect()).unwrap_or_default();
    Ok(json!({
        "schema_version": 1,
        "kind": "UNATTESTED_INCIDENT_INVESTIGATION",
        "component_id": component_id,
        "component": {
            "label": current.get("label"),
            "kind": current.get("kind"),
            "reported_state": current.get("status"),
            "source_status": current.get("source_status"),
            "source_authenticity": current.get("source_authenticity")
                .cloned().unwrap_or_else(||json!("UNKNOWN")),
            "latest_observed_at": current.get("observed_at"),
            "leading_indicators": current.get("leading_indicators")
                .cloned().unwrap_or_else(||json!([])),
            "ci_run": current.get("ci_run")
        },
        "evaluated_at": string(full,"generated_at")?,
        "observation_count":observed_count,
        "observations_included":timeline.len(),
        "older_observations_omitted":observed_count.saturating_sub(timeline.len()),
        "timeline":timeline,
        "direct_relationships":direct_relationships,
        "direct_relationships_omitted":direct.len().saturating_sub(MAX_RELATED),
        "source_evidence":source_evidence,
        "possible_dependents":possible_dependents,
        "possible_dependents_omitted":exposure.len().saturating_sub(MAX_DEPENDENTS),
        "source_history":history,
        "source_history_semantics":"SOURCE_SCAN_VERSIONS_NOT_DEPLOYMENT_EVENTS",
        "impact_semantics":"DECLARED_DEPENDENCY_POTENTIAL_IMPACT_NOT_CAUSAL_PROOF",
        "observation_semantics":"SOURCE_REPORTED_NOT_AUTHENTICATED_OR_VERIFIED",
        "root_cause":"UNDETERMINED",
        "recommended_diagnosis":"VERIFY_TELEMETRY_IDENTITY_AND_CHECK_DEPLOYMENT_LOGS",
        "runtime_discovery":full.get("runtime_discovery"),
        "execution_authorized":false
    }))
}
