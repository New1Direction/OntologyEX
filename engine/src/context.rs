//! Entity context: everything known about one entity across the business and system
//! layers, each relationship with its standing (CONFIRMED by a person, stated in SOURCE,
//! INFERRED and undecided, or an UNKNOWN gap), the decisions and evidence behind it, what
//! relies on it, and what changed recently. Mirrors `entity_context.py` rule for rule;
//! tests/rust_context_parity.py checks the two agree. Read-only: nothing here decides,
//! links or authorizes anything.

use serde_json::{json, Map, Value};
use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};

const STANDINGS: [&str; 4] = ["CONFIRMED", "SOURCE", "INFERRED", "UNKNOWN"];
const MAX_RELATIONSHIPS: usize = 200;
const MAX_IMPACT: usize = 100;
const MAX_IMPACT_DEPTH: usize = 4;
const MAX_CHANGES: usize = 25;
const MAX_SEARCH: usize = 25;
const RELIES_ON: [&str; 5] = ["DEPENDS_ON", "CALLS", "RUNS_ON", "IMPLEMENTED_BY", "CONSUMES"];
const CONFIRMED_TRACE: &str = "OBSERVED_IN_TRACES_CONFIRMED_BY_PERSON";

fn error(message: &str) -> String {
    message.to_owned()
}

fn get<'a>(v: &'a Value, key: &str) -> &'a Value {
    v.get(key).unwrap_or(&Value::Null)
}

fn arr<'a>(v: &'a Value, key: &str) -> &'a [Value] {
    v.get(key).and_then(Value::as_array).map(Vec::as_slice).unwrap_or(&[])
}

fn s(v: &Value) -> &str {
    v.as_str().unwrap_or("")
}

/// Python's truthiness for the JSON values these records hold.
fn truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64().is_none_or(|f| f != 0.0),
        Value::String(t) => !t.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

/// `str(value)` as Python's f-strings render it, for strings, None, ints and bools.
fn pystr(v: &Value) -> String {
    match v {
        Value::Null => "None".into(),
        Value::Bool(true) => "True".into(),
        Value::Bool(false) => "False".into(),
        Value::String(t) => t.clone(),
        other => other.to_string(),
    }
}

/// `a or b` for JSON values.
fn or<'a>(a: &'a Value, b: &'a Value) -> &'a Value {
    if truthy(a) { a } else { b }
}

fn rank(standing: &str) -> usize {
    STANDINGS.iter().position(|x| *x == standing).unwrap_or(STANDINGS.len())
}

fn weakest(standing: &str) -> usize {
    match standing { "CONFIRMED" => 0, "SOURCE" => 1, "INFERRED" => 2, _ => 3 }
}

/// A chain of relationships is only as strong as its weakest link (the first weakest on
/// ties); the empty chain is CONFIRMED. Same rule as `entity_context.path_standing`, whose
/// laws Lean proves; tests/conformance.py compares the tables.
pub fn path_standing<'a>(standings: impl IntoIterator<Item = &'a str>) -> &'a str {
    standings.into_iter().fold("CONFIRMED", |a, b| if weakest(b) > weakest(a) { b } else { a })
}

/// Every chain of 0..=4 standings with its path standing, in the order of
/// tests/conformance.py (last position fastest).
pub fn path_standing_rows() -> Vec<Value> {
    let mut rows = vec![json!([[], path_standing([])])];
    let mut frontier: Vec<Vec<usize>> = vec![vec![]];
    for _ in 0..4 {
        let mut next = Vec::new();
        for prefix in &frontier {
            for i in 0..STANDINGS.len() {
                let mut seq = prefix.clone();
                seq.push(i);
                rows.push(json!([seq, path_standing(seq.iter().map(|j| STANDINGS[*j]))]));
                next.push(seq);
            }
        }
        frontier = next;
    }
    rows
}

fn key_str(v: &Value) -> String {
    v.as_str().unwrap_or("").to_owned()
}

#[derive(Clone)]
struct Rel {
    source: Value,
    target: Value,
    relation: String,
    standing: &'static str,
    basis: Value,
    evidence_ids: Vec<Value>,
    decision: Value,
    via: Option<Value>,
    label: Option<Value>,
    suggestion: Option<Value>,
}

impl Rel {
    fn new(source: Value, target: Value, relation: &str, standing: &'static str, basis: Value) -> Rel {
        Rel { source, target, relation: relation.to_owned(), standing, basis, evidence_ids: vec![],
              decision: Value::Null, via: None, label: None, suggestion: None }
    }
    fn src(&self) -> &str { self.source.as_str().unwrap_or("") }
    fn tgt(&self) -> &str { self.target.as_str().unwrap_or("") }
    /// The fields a relationship row or explanation hop shows, besides its endpoints.
    fn fields(&self, out: &mut Map<String, Value>) {
        out.insert("standing".into(), json!(self.standing));
        out.insert("basis".into(), self.basis.clone());
        out.insert("evidence_ids".into(), Value::Array(self.evidence_ids.clone()));
        out.insert("decision".into(), self.decision.clone());
        for (name, value) in [("via", &self.via), ("label", &self.label), ("suggestion", &self.suggestion)] {
            if let Some(v) = value {
                if !v.is_null() { out.insert(name.into(), v.clone()); }
            }
        }
    }
}

struct Event {
    at: Value, by: Value, action: Value, note: Value, file: &'static str, summary: String,
    touches: Vec<String>, row: Value,
}

fn entities(bundle: &Value) -> BTreeMap<String, Value> {
    let state = get(bundle, "state");
    let picture = get(bundle, "business");
    let mut found = BTreeMap::new();
    for node in arr(state, "nodes") {
        let id = key_str(get(node, "id"));
        let evidence: Vec<Value> = if truthy(get(node, "evidence_id")) { vec![get(node, "evidence_id").clone()] } else { vec![] };
        found.insert(id.clone(), json!({
            "id": id, "layer": "system", "kind": get(node, "kind"), "label": get(node, "label"),
            "status": node.get("status").cloned().unwrap_or(json!("unknown")),
            "observed_at": get(node, "observed_at"), "evidence_ids": evidence}));
    }
    for (kind, field) in [("business_object", "objects"), ("business_action", "actions")] {
        for row in arr(picture, field) {
            let id = key_str(get(row, "key"));
            let quotes: Vec<Value> = arr(row, "evidence").iter().take(4).map(|e| json!({
                "path": get(e, "path"), "start": get(e, "start"), "end": get(e, "end"), "quote": get(e, "quote")})).collect();
            let rules: Vec<Value> = arr(row, "rules").iter().take(20).map(|r| json!({
                "id": get(r, "id"), "statement": get(r, "statement"), "status": get(r, "status"),
                "grade": get(r, "grade"), "grade_label": get(r, "grade_label")})).collect();
            let questions: Vec<Value> = arr(row, "blockers").iter().take(20).map(|b| json!({
                "type": get(b, "type"), "id": get(b, "id"), "statement": get(b, "statement")})).collect();
            let summary = or(or(get(row, "definition"), get(row, "verb_phrase")), &Value::Null);
            found.insert(id.clone(), json!({
                "id": id, "layer": "business", "kind": kind, "label": get(row, "id"), "skill": get(row, "skill"),
                "status": row.get("health").cloned().unwrap_or(json!("UNKNOWN")),
                "summary": if truthy(summary) { summary.clone() } else { json!("") },
                "quotes": quotes, "rules": rules, "open_questions": questions}));
        }
    }
    found
}

fn history(bundle: &Value) -> Vec<Event> {
    let mut events = Vec::new();
    for (file, field) in [("links", "link_history"), ("dependencies", "dependency_history")] {
        for event in arr(bundle, field) {
            let row = match event.get("row") { Some(r) if r.is_object() && truthy(r) => r.clone(), _ => json!({}) };
            let action = get(event, "action").clone();
            let (touches, summary) = if file == "links" {
                let (skill, binding, component) = (get(&row, "skill"), get(&row, "binding"), get(&row, "component"));
                (vec![component.clone(), json!(format!("{}:binding:{}", pystr(skill), pystr(binding)))],
                 format!("{} link {} -> {}", pystr(&action), pystr(binding), pystr(component)))
            } else {
                (vec![get(&row, "source").clone(), get(&row, "target").clone()],
                 format!("{} dependency {} -> {}", pystr(&action), pystr(get(&row, "source")), pystr(get(&row, "target"))))
            };
            events.push(Event {
                at: get(event, "at").clone(), by: get(event, "by").clone(), action,
                note: event.get("note").cloned().unwrap_or(json!("")), file, summary,
                touches: touches.iter().filter(|t| truthy(t)).map(pystr).collect(), row});
        }
    }
    events.sort_by(|a, b| (s(or(&a.at, &json!(""))), a.file, &a.summary)
        .cmp(&(s(or(&b.at, &json!(""))), b.file, &b.summary)));
    events
}

fn decision_for(history: &[Event], file: &str, matches: &[(&str, &Value)]) -> Value {
    for event in history.iter().rev() {
        if event.file == file && event.action == json!("confirm")
            && matches.iter().all(|(k, v)| get(&event.row, k) == *v) {
            return json!({"by": event.by, "at": event.at, "note": event.note});
        }
    }
    Value::Null
}

fn binding_id(binding: &Value) -> Value {
    if binding.is_object() { get(binding, "id").clone() } else { binding.clone() }
}

fn relationships(bundle: &Value, history: &[Event]) -> Vec<Rel> {
    let state = get(bundle, "state");
    let picture = get(bundle, "business");
    let mut out = Vec::new();
    for edge in arr(state, "edges") {
        let confirmed = get(edge, "state") == &json!(CONFIRMED_TRACE);
        let mut rel = Rel::new(get(edge, "source").clone(), get(edge, "target").clone(), s(get(edge, "relation")),
                               if confirmed { "CONFIRMED" } else { "SOURCE" },
                               or(get(edge, "state"), &json!("DECLARED_IN_SOURCE")).clone());
        if truthy(get(edge, "evidence_id")) { rel.evidence_ids = vec![get(edge, "evidence_id").clone()]; }
        if confirmed {
            rel.decision = decision_for(history, "dependencies",
                                        &[("source", get(edge, "source")), ("target", get(edge, "target"))]);
        }
        out.push(rel);
    }
    let mut decided: HashMap<(String, String, String), &Value> = HashMap::new();
    for r in arr(picture, "link_decided") {
        if matches!(s(get(r, "decision")), "confirm" | "confirmed") {
            decided.insert((key_str(get(r, "skill")), key_str(get(r, "binding")), key_str(get(r, "component"))), r);
        }
    }
    let mut proposers: HashMap<(String, String, String), &Value> = HashMap::new();
    for p in arr(bundle, "agent_proposals") {
        proposers.entry((key_str(get(p, "skill")), key_str(get(p, "binding")), key_str(get(p, "component"))))
            .or_insert(get(p, "proposed_by"));
    }
    for (kind, field) in [("action", "actions"), ("object", "objects")] {
        for row in arr(picture, field) {
            for binding in arr(row, "bindings") {
                let id = binding_id(binding);
                let components = if binding.is_object() { arr(binding, "components") } else { arr(row, "systems") };
                for component in components {
                    let mut rel = Rel::new(get(row, "key").clone(), component.clone(),
                                           if kind == "action" { "RUNS_ON" } else { "IMPLEMENTED_BY" },
                                           "CONFIRMED", json!("PERSON_CONFIRMED_LINK"));
                    rel.via = Some(json!(format!("{}:binding:{}", pystr(get(row, "skill")), pystr(&id))));
                    let found = decision_for(history, "links", &[("skill", get(row, "skill")), ("binding", &id),
                                                                 ("component", component)]);
                    rel.decision = if truthy(&found) { found } else {
                        match decided.get(&(key_str(get(row, "skill")), key_str(&id), key_str(component))) {
                            Some(link) => json!({"by": get(link, "declared_by"), "at": null,
                                                 "note": link.get("note").cloned().unwrap_or(json!(""))}),
                            None => Value::Null,
                        }
                    };
                    let key = (key_str(get(row, "skill")), key_str(&id), key_str(component));
                    if let (Some(decision), Some(by)) = (rel.decision.as_object_mut(), proposers.get(&key)) {
                        decision.insert("proposed_by".into(), (*by).clone());
                    }
                    out.push(rel);
                }
            }
        }
    }
    for edge in arr(get(picture, "graph"), "edges") {
        let relation = s(get(edge, "relation"));
        if relation == "RUNS_ON" || relation == "IMPLEMENTED_BY" { continue; }
        let mut rel = Rel::new(get(edge, "source").clone(), get(edge, "target").clone(), relation, "SOURCE",
                               json!("DECLARED_IN_DOMAIN_SKILL"));
        rel.label = Some(get(edge, "label").clone());
        out.push(rel);
    }
    let actions = arr(picture, "actions");
    for row in arr(get(picture, "link_suggestions"), "suggestions") {
        let key = format!("{}:binding:{}", pystr(get(row, "skill")), pystr(get(row, "binding")));
        let mut sources: Vec<Value> = actions.iter().filter(|a| get(a, "skill") == get(row, "skill")
            && arr(a, "bindings").iter().any(|b| binding_id(b) == *get(row, "binding")))
            .map(|a| get(a, "key").clone()).collect();
        if sources.is_empty() { sources.push(json!(key)); }
        let signals: Vec<Value> = arr(row, "signals").iter().map(|x| or(get(x, "detail"), get(x, "signal")).clone())
            .take(6).collect();
        for source in sources {
            let mut rel = Rel::new(source, get(row, "component").clone(), "RUNS_ON", "INFERRED", json!("LINK_SUGGESTION"));
            rel.via = Some(json!(key));
            rel.suggestion = Some(json!({"id": get(row, "id"), "strength": get(row, "strength"), "signals": signals,
                                         "proposed_by": row.get("proposed_by").cloned().unwrap_or(json!("ranked suggestion"))}));
            out.push(rel);
        }
    }
    for row in arr(get(picture, "dependency_suggestions"), "suggestions") {
        let calls = row.get("calls").cloned().unwrap_or(json!(0));
        let mut rel = Rel::new(get(row, "source").clone(), get(row, "target").clone(), "CALLS", "INFERRED",
                               json!("SEEN_IN_TRACES_NOT_CONFIRMED"));
        rel.suggestion = Some(json!({"id": get(row, "id"), "strength": get(row, "strength"),
            "signals": [format!("{} calls, last seen {}", pystr(&calls), pystr(get(row, "last_seen")))],
            "proposed_by": "trace observation"}));
        out.push(rel);
    }
    for gap in arr(picture, "link_gaps") {
        let link = match gap.get("link") { Some(l) if truthy(l) => l.clone(), _ => json!({}) };
        out.push(Rel::new(json!(format!("{}:binding:{}", pystr(get(&link, "skill")), pystr(get(&link, "binding")))),
                          get(&link, "component").clone(), "RUNS_ON", "UNKNOWN", get(gap, "reason").clone()));
    }
    out
}

fn side(entities: &BTreeMap<String, Value>, id: &Value) -> Value {
    match id.as_str().and_then(|i| entities.get(i)) {
        Some(e) => json!({"id": e["id"], "layer": e["layer"], "kind": e["kind"], "label": e["label"]}),
        None => json!({"id": id, "layer": "unknown", "kind": "unknown",
                       "label": if truthy(id) { id.clone() } else { json!("") }}),
    }
}

fn evidence(bundle: &Value, ids: &BTreeSet<String>) -> Vec<Value> {
    let records: HashMap<&str, &Value> = arr(get(bundle, "state"), "source_evidence").iter()
        .map(|e| (s(get(e, "id")), e)).collect();
    ids.iter().filter_map(|i| records.get(i.as_str()).map(|r| json!({
        "id": get(r, "id"), "path": get(r, "path"), "sha256": get(r, "sha256"),
        "source_kind": get(r, "source_kind"), "authenticity": get(r, "authenticity")}))).collect()
}

fn impact(entity_id: &str, rels: &[Rel], entities: &BTreeMap<String, Value>) -> Vec<Value> {
    let mut relying: HashMap<&str, Vec<&Rel>> = HashMap::new();
    for rel in rels {
        if RELIES_ON.contains(&rel.relation.as_str()) && rel.standing != "UNKNOWN" {
            relying.entry(rel.tgt()).or_default().push(rel);
        }
    }
    let mut found: Vec<Value> = Vec::new();
    let mut frontier: std::collections::VecDeque<(String, usize, &str, Vec<String>)> =
        std::collections::VecDeque::from([(entity_id.to_owned(), 0, "CONFIRMED", vec![])]);
    let mut seen: HashSet<String> = HashSet::from([entity_id.to_owned()]);
    while found.len() < MAX_IMPACT {
        let Some((current, depth, standing, path)) = frontier.pop_front() else { break };
        if depth >= MAX_IMPACT_DEPTH { continue; }
        let mut next: Vec<&Rel> = relying.get(current.as_str()).cloned().unwrap_or_default();
        next.sort_by(|a, b| (a.src(), &a.relation, a.standing).cmp(&(b.src(), &b.relation, b.standing)));
        for rel in next {
            let source = pystr(&rel.source);
            if seen.contains(&source) { continue; }
            seen.insert(source.clone());
            let step = path_standing([standing, rel.standing]);
            let mut hop = path.clone();
            hop.push(format!("{} {} {}", source, rel.relation, current));
            let mut row = side(entities, &rel.source).as_object().cloned().unwrap_or_default();
            row.insert("depth".into(), json!(depth + 1));
            row.insert("standing".into(), json!(step));
            row.insert("path".into(), json!(hop));
            found.push(Value::Object(row));
            frontier.push_back((source, depth + 1, step, hop));
        }
    }
    found.truncate(MAX_IMPACT);
    found
}

fn check_bundle(bundle: &Value) -> Result<(), String> {
    if !bundle.is_object() || !get(bundle, "state").is_object() { return Err(error("expected a context bundle")); }
    Ok(())
}

pub fn context(bundle: &Value, entity_id: &str) -> Result<Value, String> {
    check_bundle(bundle)?;
    if get(get(bundle, "state"), "execution_authorized") != &json!(false) {
        return Err(error("the state must not authorize execution"));
    }
    if entity_id.is_empty() || entity_id.chars().count() > 1024 { return Err(error("invalid entity ID")); }
    let entities = entities(bundle);
    let history = history(bundle);
    let rels = relationships(bundle, &history);
    let entity = entities.get(entity_id).ok_or_else(|| error("entity not found"))?;
    let mut mine: Vec<&Rel> = rels.iter().filter(|r| r.src() == entity_id || r.tgt() == entity_id).collect();
    mine.sort_by(|a, b| (rank(a.standing), &a.relation, a.src(), a.tgt())
        .cmp(&(rank(b.standing), &b.relation, b.src(), b.tgt())));
    let mut evidence_ids: BTreeSet<String> = arr(entity, "evidence_ids").iter().map(pystr).collect();
    let mut rows: Vec<Value> = Vec::new();
    for rel in mine.iter().take(MAX_RELATIONSHIPS) {
        let outgoing = rel.src() == entity_id;
        evidence_ids.extend(rel.evidence_ids.iter().map(pystr));
        let mut row = Map::new();
        row.insert("direction".into(), json!(if outgoing { "out" } else { "in" }));
        row.insert("relation".into(), json!(rel.relation));
        row.insert("other".into(), side(&entities, if outgoing { &rel.target } else { &rel.source }));
        rel.fields(&mut row);
        rows.push(Value::Object(row));
    }
    let mut vias: HashSet<String> = mine.iter().filter_map(|r| r.via.as_ref()).filter(|v| truthy(v))
        .map(pystr).collect();
    vias.insert(entity_id.to_owned());
    let changes: Vec<Value> = history.iter().rev().filter(|e| e.touches.iter().any(|t| vias.contains(t)))
        .take(MAX_CHANGES).map(|e| json!({"at": e.at, "by": e.by, "action": e.action, "note": e.note,
                                          "file": e.file, "summary": e.summary})).collect();
    let business = s(get(entity, "layer")) == "business";
    let mut why: Vec<Value> = Vec::new();
    if business && truthy(get(entity, "summary")) {
        why.push(json!(format!("{}: {} (declared in the {} skill)", pystr(get(entity, "label")),
                               pystr(get(entity, "summary")), pystr(get(entity, "skill")))));
    }
    let incoming: Vec<Value> = rows.iter().filter(|r| s(get(r, "direction")) == "in"
            && matches!(s(get(r, "standing")), "CONFIRMED" | "SOURCE")).take(12).map(|r| {
        let decision = get(r, "decision");
        let mut text = format!("{} {} this ({}", pystr(get(get(r, "other"), "label")),
                               s(get(r, "relation")).to_lowercase().replace('_', " "), s(get(r, "standing")).to_lowercase());
        if truthy(decision) {
            text.push_str(&format!(", confirmed by {}", pystr(get(decision, "by"))));
            if truthy(get(decision, "at")) {
                text.push_str(&format!(" on {}", pystr(get(decision, "at")).chars().take(10).collect::<String>()));
            }
        }
        text.push(')');
        json!(text)
    }).collect();
    why.extend(incoming);
    let mut unknowns: Vec<Value> = rows.iter().filter(|r| s(get(r, "standing")) == "UNKNOWN").cloned().collect();
    for question in arr(entity, "open_questions") {
        let kind = s(get(question, "type"));
        if kind == "unknown" || kind == "conflict" {
            unknowns.push(json!({"direction": "out", "relation": "OPEN_QUESTION",
                "other": {"id": get(question, "id"), "layer": "business", "kind": kind, "label": get(question, "statement")},
                "standing": "UNKNOWN", "basis": format!("SKILL_{}", kind.to_uppercase()), "evidence_ids": [], "decision": null}));
        }
    }
    if business && !rows.iter().any(|r| matches!(s(get(r, "relation")), "RUNS_ON" | "IMPLEMENTED_BY")
                                     && s(get(r, "standing")) == "CONFIRMED") {
        unknowns.push(json!({"direction": "out", "relation": "RUNS_ON",
            "other": {"id": "", "layer": "unknown", "kind": "unknown", "label": "no confirmed system"},
            "standing": "UNKNOWN", "basis": "NO_CONFIRMED_LINK", "evidence_ids": [], "decision": null}));
    }
    let counts: Map<String, Value> = STANDINGS.iter()
        .map(|st| (st.to_string(), json!(rows.iter().filter(|r| s(get(r, "standing")) == *st).count()))).collect();
    let suggestions: Vec<Value> = rows.iter().filter(|r| s(get(r, "standing")) == "INFERRED" && r.get("suggestion").is_some())
        .cloned().collect();
    let mut shown = entity.as_object().cloned().unwrap_or_default();
    shown.remove("evidence_ids");
    Ok(json!({"schema_version": 1, "kind": "ENTITY_CONTEXT", "entity": shown,
              "relationships": rows, "relationships_total": mine.len(),
              "relationships_omitted": mine.len().saturating_sub(MAX_RELATIONSHIPS),
              "standing_counts": counts, "why": why, "relied_on_by": impact(entity_id, &rels, &entities),
              "suggestions": suggestions, "unknowns": unknowns, "recent_changes": changes,
              "source_evidence": evidence(bundle, &evidence_ids),
              "standing_semantics": "CONFIRMED_BY_PERSON|SOURCE_DECLARED|INFERRED_NOT_DECIDED|UNKNOWN_GAP",
              "impact_semantics": "DECLARED_REACHABILITY_NOT_MEASURED_BLAST_RADIUS",
              "execution_authorized": false}))
}

pub fn explain(bundle: &Value, source: &str, target: &str) -> Result<Value, String> {
    check_bundle(bundle)?;
    let entities = entities(bundle);
    if !entities.contains_key(source) || !entities.contains_key(target) { return Err(error("entity not found")); }
    let history = history(bundle);
    let rels = relationships(bundle, &history);
    let mut adjacency: HashMap<String, Vec<(Value, &Rel)>> = HashMap::new();
    for rel in &rels {
        adjacency.entry(pystr(&rel.source)).or_default().push((rel.target.clone(), rel));
        adjacency.entry(pystr(&rel.target)).or_default().push((rel.source.clone(), rel));
    }
    let hop = |node: &Value, other: &Value, rel: &Rel| -> Value {
        let mut out = Map::new();
        out.insert("from".into(), side(&entities, node));
        out.insert("to".into(), side(&entities, other));
        out.insert("relation".into(), json!(rel.relation));
        out.insert("direction".into(), json!(if rel.source == *node { "out" } else { "in" }));
        rel.fields(&mut out);
        out.remove("label");
        Value::Object(out)
    };
    let mut paths: Vec<Vec<Value>> = Vec::new();
    let mut frontier: Vec<(Value, Vec<Value>, HashSet<String>)> =
        vec![(json!(source), vec![], HashSet::from([source.to_owned()]))];
    for _ in 0..3 {
        let mut next = Vec::new();
        for (node, hops, seen) in &frontier {
            let mut around: Vec<&(Value, &Rel)> = adjacency.get(&pystr(node)).map(|v| v.iter().collect()).unwrap_or_default();
            around.sort_by(|a, b| (s(&a.0), &a.1.relation, a.1.standing).cmp(&(s(&b.0), &b.1.relation, b.1.standing)));
            for (other, rel) in around {
                let other_key = pystr(other);
                if seen.contains(&other_key) { continue; }
                let mut extended = hops.clone();
                extended.push(hop(node, other, rel));
                if other.as_str() == Some(target) {
                    paths.push(extended);
                } else {
                    let mut seen2 = seen.clone();
                    seen2.insert(other_key.clone());
                    next.push((other.clone(), extended, seen2));
                }
            }
        }
        next.truncate(2000);
        frontier = next;
        if paths.len() >= 10 { break; }
    }
    let worst = |p: &Vec<Value>| weakest(path_standing(p.iter().map(|h| s(get(h, "standing")))));
    paths.sort_by_key(|p| (p.len(), worst(p)));
    let out: Vec<Value> = paths.iter().take(10).map(|p| {
        let standing = path_standing(p.iter().map(|h| s(get(h, "standing"))));
        json!({"standing": standing, "hops": p})
    }).collect();
    let ids: BTreeSet<String> = out.iter().flat_map(|p| arr(p, "hops").iter()
        .flat_map(|h| arr(h, "evidence_ids").iter().map(pystr)).collect::<Vec<_>>()).collect();
    Ok(json!({"schema_version": 1, "kind": "RELATIONSHIP_EXPLANATION",
              "source": side(&entities, &json!(source)), "target": side(&entities, &json!(target)),
              "paths": out,
              "verdict": out.first().map(|p| p["standing"].clone()).unwrap_or(json!("NO_PATH_WITHIN_THREE_HOPS")),
              "source_evidence": evidence(bundle, &ids),
              "standing_semantics": "A_PATH_IS_AS_STRONG_AS_ITS_WEAKEST_HOP", "execution_authorized": false}))
}

pub fn search(bundle: &Value, query: &str) -> Result<Value, String> {
    check_bundle(bundle)?;
    let length = query.chars().count();
    if length == 0 || length > 200 || query.chars().any(|c| c < ' ' || c == '\u{7f}') {
        return Err(error("invalid query"));
    }
    let lowered = query.to_lowercase();
    let words: Vec<&str> = lowered.split_whitespace().collect();
    let mut hits: Vec<Value> = entities(bundle).values().filter(|e| {
        let text = format!("{} {} {}", pystr(get(e, "label")), pystr(get(e, "id")),
                           pystr(e.get("summary").unwrap_or(&json!("")))).to_lowercase();
        words.iter().all(|w| text.contains(w))
    }).map(|e| json!({"id": e["id"], "layer": e["layer"], "kind": e["kind"], "label": e["label"],
                      "status": get(e, "status")})).collect();
    hits.sort_by(|a, b| (s(get(a, "layer")) != "business", s(get(a, "label")).chars().count(), s(get(a, "id")))
        .cmp(&(s(get(b, "layer")) != "business", s(get(b, "label")).chars().count(), s(get(b, "id")))));
    let total = hits.len();
    hits.truncate(MAX_SEARCH);
    Ok(json!({"schema_version": 1, "kind": "ENTITY_SEARCH", "query": query, "results": hits,
              "total": total, "execution_authorized": false}))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn bundle() -> Value {
        json!({
            "state": {"execution_authorized": false,
                "nodes": [
                    {"id": "svc:api", "kind": "declared_service", "label": "api", "status": "ok", "evidence_id": "e1"},
                    {"id": "svc:db", "kind": "declared_service", "label": "db", "status": "error", "evidence_id": "e1"}],
                "edges": [{"source": "svc:api", "target": "svc:db", "relation": "DEPENDS_ON", "evidence_id": "e1"}],
                "source_evidence": [{"id": "e1", "path": "compose.yml", "sha256": "ab", "source_kind": "MANIFEST"}]},
            "business": {
                "actions": [{"key": "s:action:Pay", "id": "Pay", "skill": "s", "verb_phrase": "take a payment",
                             "bindings": [{"id": "b.pay", "components": ["svc:api"]}], "health": "AT_RISK"}],
                "objects": [], "graph": {"edges": []},
                "link_decided": [{"skill": "s", "binding": "b.pay", "component": "svc:api", "decision": "confirm",
                                  "declared_by": "Ana", "note": "handler"}],
                "link_suggestions": {"suggestions": [{"id": "x1", "skill": "s", "binding": "b.pay", "component": "svc:db",
                                                      "strength": "WEAK", "signals": [{"signal": "SHARED_NAME"}]}]}},
            "link_history": [{"at": "2026-10-01T10:00:00Z", "by": "Ana", "action": "confirm", "note": "handler",
                              "row": {"skill": "s", "binding": "b.pay", "component": "svc:api"}}]
        })
    }

    #[test]
    fn confirmed_links_carry_who_and_when_and_suggestions_stay_inferred() {
        let c = context(&bundle(), "s:action:Pay").unwrap();
        let rows = c["relationships"].as_array().unwrap();
        assert_eq!(rows[0]["standing"], "CONFIRMED");
        assert_eq!(rows[0]["decision"]["by"], "Ana");
        assert_eq!(rows[0]["decision"]["at"], "2026-10-01T10:00:00Z");
        assert!(rows.iter().any(|r| r["standing"] == "INFERRED" && r["suggestion"]["id"] == "x1"));
        assert_eq!(c["recent_changes"][0]["by"], "Ana");
        assert_eq!(c["execution_authorized"], false);
    }

    #[test]
    fn impact_reaches_the_action_through_a_declared_dependency() {
        let c = context(&bundle(), "svc:db").unwrap();
        let labels: Vec<&str> = c["relied_on_by"].as_array().unwrap().iter().map(|r| r["label"].as_str().unwrap()).collect();
        // Pay relies on db directly through the undecided suggestion (inferred), and api through a declared dependency.
        assert_eq!(labels, ["Pay", "api"]);
        assert_eq!(c["relied_on_by"][0]["standing"], "INFERRED");
        assert_eq!(c["relied_on_by"][1]["standing"], "SOURCE");
    }

    #[test]
    fn explain_names_the_path_and_its_weakest_hop() {
        let e = explain(&bundle(), "s:action:Pay", "svc:db").unwrap();
        assert_eq!(e["paths"][0]["hops"].as_array().unwrap().len(), 1);  // the inferred suggestion is one hop
        assert_eq!(e["paths"][0]["standing"], "INFERRED");
        assert_eq!(e["paths"][1]["standing"], "SOURCE");                  // confirmed link, then declared dependency
    }

    #[test]
    fn refusals() {
        assert!(context(&bundle(), "missing").is_err());
        assert!(search(&bundle(), "bad\u{1}query").is_err());
        let mut authorizing = bundle();
        authorizing["state"]["execution_authorized"] = json!(true);
        assert!(context(&authorizing, "svc:db").is_err());
    }
}
