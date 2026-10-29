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
/// Edge states a person's decision produced: a call seen in traces, or a dependency an agent proposed.
const CONFIRMED_EDGE_STATES: [&str; 2] = ["OBSERVED_IN_TRACES_CONFIRMED_BY_PERSON", "PROPOSED_BY_AGENT_CONFIRMED_BY_PERSON"];

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

/// Compare the evidence a decision recorded with the evidence now (`entity_context.evidence_check`):
/// CURRENT, CHANGED_SINCE_DECISION (with the changed paths), or NOT_RECORDED.
pub fn evidence_check(recorded: &Value, now: &Value) -> Value {
    let Some(recorded) = recorded.as_object() else { return json!({"status": "NOT_RECORDED", "changed": []}) };
    let empty = Map::new();
    let now = now.as_object().unwrap_or(&empty);
    let paths: BTreeSet<&String> = recorded.keys().chain(now.keys()).collect();
    let changed: Vec<&String> = paths.into_iter().filter(|p| recorded.get(*p) != now.get(*p)).take(16).collect();
    json!({"status": if changed.is_empty() { "CURRENT" } else { "CHANGED_SINCE_DECISION" }, "changed": changed})
}

/// A confirmation reads as CONFIRMED only while its evidence is unchanged (Lean: `linkStanding`).
pub fn watermark_standing(recorded: &Value, now: &Value) -> &'static str {
    if evidence_check(recorded, now)["status"] == "CHANGED_SINCE_DECISION" { "INFERRED" } else { "CONFIRMED" }
}

/// Recorded evidence (0: none, 1 or 2: value 0 or 1) against evidence now (0 or 1), in the
/// order of tests/conformance.py and proofs/lean/Conformance.lean.
pub fn watermark_rows() -> Vec<Value> {
    let mut rows = Vec::new();
    for recorded in 0..3u32 {
        for now in 0..2u32 {
            let then = if recorded == 0 { Value::Null } else { json!({"f": (recorded - 1).to_string()}) };
            rows.push(json!([[recorded, now], watermark_standing(&then, &json!({"f": now.to_string()}))]));
        }
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
    evidence_check: Option<Value>,
}

impl Rel {
    fn new(source: Value, target: Value, relation: &str, standing: &'static str, basis: Value) -> Rel {
        Rel { source, target, relation: relation.to_owned(), standing, basis, evidence_ids: vec![],
              decision: Value::Null, via: None, label: None, suggestion: None, evidence_check: None }
    }
    fn src(&self) -> &str { self.source.as_str().unwrap_or("") }
    fn tgt(&self) -> &str { self.target.as_str().unwrap_or("") }
    /// The fields a relationship row or explanation hop shows, besides its endpoints.
    fn fields(&self, out: &mut Map<String, Value>) {
        out.insert("standing".into(), json!(self.standing));
        out.insert("basis".into(), self.basis.clone());
        out.insert("evidence_ids".into(), Value::Array(self.evidence_ids.clone()));
        out.insert("decision".into(), self.decision.clone());
        for (name, value) in [("via", &self.via), ("label", &self.label), ("suggestion", &self.suggestion),
                              ("evidence_check", &self.evidence_check)] {
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
                "path": get(e, "path"), "start": get(e, "start_line"), "end": get(e, "end_line"), "quote": get(e, "quote")})).collect();
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
                "quotes": quotes, "rules": rules, "open_questions": questions,
                // Rules a person confirmed from agent proposals, and ones still proposed.
                "confirmed_rules": arr(row, "person_rules").iter().take(20).cloned().collect::<Vec<_>>(),
                "proposed_rules": arr(row, "rule_proposals").iter().take(20).cloned().collect::<Vec<_>>()}));
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
        if event.file == file && (event.action == json!("confirm") || event.action == json!("reconfirm"))
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
    let mut dependency_proposers: HashMap<(String, String), &Value> = HashMap::new();
    for p in arr(bundle, "agent_dependency_proposals") {
        dependency_proposers.entry((key_str(get(p, "source")), key_str(get(p, "target")))).or_insert(get(p, "proposed_by"));
    }
    for edge in arr(state, "edges") {
        let confirmed = get(edge, "state").as_str().is_some_and(|x| CONFIRMED_EDGE_STATES.contains(&x));
        let mut rel = Rel::new(get(edge, "source").clone(), get(edge, "target").clone(), s(get(edge, "relation")),
                               if confirmed { "CONFIRMED" } else { "SOURCE" },
                               or(get(edge, "state"), &json!("DECLARED_IN_SOURCE")).clone());
        if truthy(get(edge, "evidence_id")) { rel.evidence_ids = vec![get(edge, "evidence_id").clone()]; }
        if confirmed {
            rel.decision = decision_for(history, "dependencies",
                                        &[("source", get(edge, "source")), ("target", get(edge, "target"))]);
            let pair = (key_str(get(edge, "source")), key_str(get(edge, "target")));
            if let (Some(decision), Some(by)) = (rel.decision.as_object_mut(), dependency_proposers.get(&pair)) {
                decision.insert("proposed_by".into(), (*by).clone());
            }
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
    let mut marks: HashMap<(String, String, String), &Value> = HashMap::new();
    for m in arr(picture, "link_watermarks") {
        marks.entry((key_str(get(m, "skill")), key_str(get(m, "binding")), key_str(get(m, "component")))).or_insert(m);
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
                    if let Some(mark) = marks.get(&key) {
                        let mut check = evidence_check(get(mark, "recorded"), get(mark, "now"));
                        check["decision_id"] = get(mark, "id").clone();
                        if check["status"] == "CHANGED_SINCE_DECISION" {
                            rel.standing = "INFERRED";
                            rel.basis = json!("CONFIRMED_BEFORE_EVIDENCE_CHANGED");
                        }
                        rel.evidence_check = Some(check);
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
        let traced = get(row, "evidence").as_object().is_some_and(|e| e.contains_key("calls"));
        let mut rel = Rel::new(get(row, "source").clone(), get(row, "target").clone(),
                               if traced { "CALLS" } else { "DEPENDS_ON" }, "INFERRED",
                               json!(if traced { "SEEN_IN_TRACES_NOT_CONFIRMED" } else { "AGENT_PROPOSAL_NOT_CONFIRMED" }));
        let signals: Vec<Value> = arr(row, "signals").iter().map(|x| or(get(x, "detail"), get(x, "signal")).clone())
            .take(6).collect();
        rel.suggestion = Some(json!({"id": get(row, "id"), "strength": get(row, "strength"), "signals": signals,
            "proposed_by": row.get("proposed_by").cloned().unwrap_or(json!("trace observation"))}));
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

/// What relies on this entity, transitively: each through its strongest chain of 1 to
/// MAX_IMPACT_DEPTH hops (weakest hop decides), the fewest hops among equals, then the first
/// found. Same as `entity_context._impact`; Lean proves the layered computation against the
/// strongest-chain statement (proofs/lean/OntologyEXSpec/Impact.lean).
fn impact(entity_id: &str, rels: &[Rel], entities: &BTreeMap<String, Value>) -> Vec<Value> {
    let mut relying: HashMap<&str, Vec<&Rel>> = HashMap::new();
    for rel in rels {
        if RELIES_ON.contains(&rel.relation.as_str()) && rel.standing != "UNKNOWN" {
            relying.entry(rel.tgt()).or_default().push(rel);
        }
    }
    for rows in relying.values_mut() {
        rows.sort_by(|a, b| (a.src(), &a.relation, a.standing).cmp(&(b.src(), &b.relation, b.standing)));
    }
    // (entity, weakness, hops, path), in the order first reached; `index` finds an entity's row.
    let mut best: Vec<(String, usize, usize, Vec<String>)> = Vec::new();
    let mut index: HashMap<String, usize> = HashMap::new();
    let mut values: HashMap<String, &Value> = HashMap::new();  // the entity ID as the relationship gives it
    let mut layer: Vec<(String, usize, Vec<String>)> = vec![(entity_id.to_owned(), weakest("CONFIRMED"), vec![])];
    for hops in 1..=MAX_IMPACT_DEPTH {
        let mut reached: Vec<(String, usize, Vec<String>)> = Vec::new();
        let mut at: HashMap<String, usize> = HashMap::new();
        for (current, weakness, path) in &layer {
            for rel in relying.get(current.as_str()).map(Vec::as_slice).unwrap_or_default() {
                let source = pystr(&rel.source);
                values.entry(source.clone()).or_insert(&rel.source);
                let step = (*weakness).max(weakest(rel.standing));
                let stronger = match at.get(&source) { Some(&i) => step < reached[i].1, None => true };
                if stronger {
                    let mut hop = path.clone();
                    hop.push(format!("{} {} {}", source, rel.relation, current));
                    match at.get(&source) {
                        Some(&i) => reached[i] = (source, step, hop),
                        None => { at.insert(source.clone(), reached.len()); reached.push((source, step, hop)); }
                    }
                }
            }
        }
        for (source, weakness, path) in &reached {
            if source == entity_id { continue; }
            match index.get(source) {
                Some(&i) => if *weakness < best[i].1 { best[i] = (source.clone(), *weakness, hops, path.clone()); },
                None => { index.insert(source.clone(), best.len()); best.push((source.clone(), *weakness, hops, path.clone())); }
            }
        }
        layer = reached;
    }
    best.sort_by_key(|row| row.2);  // stable: by hops, then the order first reached
    best.into_iter().take(MAX_IMPACT).map(|(source, weakness, hops, path)| {
        let mut row = side(entities, values[&source]).as_object().cloned().unwrap_or_default();
        row.insert("depth".into(), json!(hops));
        row.insert("standing".into(), json!(STANDINGS[weakness]));
        row.insert("path".into(), json!(path));
        Value::Object(row)
    }).collect()
}

const MAX_CHANGED_FILES: usize = 2000;
const MAX_RANGES: usize = 200;
const MAX_TOUCHED: usize = 500;

fn clean(v: &Value) -> bool {
    v.as_str().is_some_and(|t| { let n = t.chars().count(); n > 0 && n <= 1024 && t.chars().all(|c| c >= ' ' && c != '\x7f') })
}

fn int(v: &Value) -> Option<i64> {
    if v.is_i64() || v.is_u64() { v.as_i64() } else { None }
}

type Change<'a> = (&'a str, Option<Vec<(i64, i64)>>);
type Changes<'a> = (Vec<Change<'a>>, Vec<(String, String)>);

/// Validate a change set the way `entity_context._changes` does.
fn changes_of(changes: &Value) -> Result<Changes<'_>, String> {
    let invalid = |m: &str| Err(error(m));
    let Some(obj) = changes.as_object() else { return invalid("changes need files and/or bindings") };
    if obj.keys().any(|k| k != "files" && k != "bindings") { return invalid("changes need files and/or bindings"); }
    let files: &[Value] = match obj.get("files") { None => &[], Some(Value::Array(a)) => a, _ => return invalid("too many changed files") };
    if files.len() > MAX_CHANGED_FILES { return invalid("too many changed files"); }
    let mut out = Vec::new();
    let mut seen = HashSet::new();
    for row in files {
        let Some(r) = row.as_object() else { return invalid("invalid changed file") };
        let status_ok = match r.get("status") { None => true, Some(v) => matches!(v.as_str(), Some("A" | "M" | "D" | "R")) };
        let path = r.get("path").unwrap_or(&Value::Null);
        if r.keys().any(|k| k != "path" && k != "status" && k != "ranges") || !clean(path) || !status_ok
            || !seen.insert(path.as_str().unwrap_or("")) { return invalid("invalid changed file"); }
        let ranges = match r.get("ranges") {
            None | Some(Value::Null) => None,
            Some(Value::Array(list)) if list.len() <= MAX_RANGES => {
                let mut got = Vec::new();
                for item in list {
                    match item.as_array().map(|x| x.as_slice()) {
                        Some([a, b]) => match (int(a), int(b)) {
                            (Some(a), Some(b)) if a >= 1 && b >= 1 && a <= b => got.push((a, b)),
                            _ => return invalid("invalid changed line ranges"),
                        },
                        _ => return invalid("invalid changed line ranges"),
                    }
                }
                Some(got)
            }
            _ => return invalid("invalid changed line ranges"),
        };
        out.push((path.as_str().unwrap_or(""), ranges));
    }
    let bindings: &[Value] = match obj.get("bindings") { None => &[], Some(Value::Array(a)) => a, _ => return invalid("invalid changed bindings") };
    if bindings.len() > MAX_CHANGED_FILES { return invalid("invalid changed bindings"); }
    let mut pairs = BTreeSet::new();
    for b in bindings {
        let Some(o) = b.as_object() else { return invalid("invalid changed bindings") };
        if o.len() != 2 || !clean(get(b, "skill")) || !clean(get(b, "binding")) { return invalid("invalid changed bindings"); }
        pairs.insert((s(get(b, "skill")).to_owned(), s(get(b, "binding")).to_owned()));
    }
    out.sort_by(|a, b| a.0.cmp(b.0));
    Ok((out, pairs.into_iter().collect()))
}

fn hit(ranges: &Option<Vec<(i64, i64)>>, start: &Value, end: &Value) -> Value {
    let (Some(ranges), Some(start)) = (ranges, int(start)) else { return Value::Null };
    let end = int(end).filter(|e| *e >= start).unwrap_or(start);
    json!(ranges.iter().any(|(a, b)| *a <= end && start <= *b))
}

fn names(cited: &Value, changed: &str) -> bool {
    cited.as_str().is_some_and(|c| !c.is_empty() && (changed == c || changed.ends_with(&format!("/{c}"))))
}

/// What a change touches, and what relies on what it touches. Mirrors
/// `entity_context.change_impact`; the change set is the bundle's "changes" field.
pub fn change_impact(bundle: &Value) -> Result<Value, String> {
    check_bundle(bundle)?;
    if get(get(bundle, "state"), "execution_authorized") != &json!(false) {
        return Err(error("context never reads an authorizing state"));
    }
    let (files, bindings) = changes_of(get(bundle, "changes"))?;
    let ents = entities(bundle);
    let picture = get(bundle, "business");
    let paths: HashMap<String, &Value> = arr(get(bundle, "state"), "source_evidence").iter()
        .map(|e| (pystr(get(e, "id")), get(e, "path"))).collect();
    let mut holders: HashMap<(String, String), Vec<String>> = HashMap::new();
    let mut cited: BTreeMap<(String, String), Vec<&Value>> = BTreeMap::new();
    for row in arr(picture, "actions").iter().chain(arr(picture, "objects")) {
        for binding in arr(row, "bindings") {
            let key = (key_str(get(row, "skill")), key_str(&binding_id(binding)));
            holders.entry(key.clone()).or_default().push(pystr(get(row, "key")));
            if binding.is_object() { cited.entry(key).or_default().extend(arr(binding, "evidence")); }
        }
    }
    let mut touches: BTreeMap<String, BTreeMap<(String, String), Value>> = BTreeMap::new();
    let mut mapped: HashSet<String> = HashSet::new();  // a file counts as mapped only when a touch lands
    let mut touch = |id: &str, reason: &str, path: &str, line_hit: Value| {
        if !ents.contains_key(id) { return; }
        mapped.insert(path.to_owned());
        let seen = touches.entry(id.to_owned()).or_default();
        match seen.get_mut(&(reason.to_owned(), path.to_owned())) {
            None => { seen.insert((reason.to_owned(), path.to_owned()),
                                  json!({"reason": reason, "path": path, "line_hit": line_hit})); }
            // Any changed cited line wins; a known miss beats unknown. Order-independent.
            Some(old) => if line_hit == json!(true) || (line_hit == json!(false) && old["line_hit"].is_null()) {
                old["line_hit"] = line_hit;
            },
        }
    };
    let mut reconfirm = Vec::new();
    for (path, ranges) in &files {
        for node in arr(get(bundle, "state"), "nodes") {
            let declared = node.get("evidence_id").and_then(|e| paths.get(&pystr(e))).and_then(|p| p.as_str());
            if declared == Some(path) {
                touch(&pystr(get(node, "id")), "DECLARED_IN_CHANGED_FILE", path, hit(ranges, get(node, "source_line"), &Value::Null));
            }
        }
        for entity in ents.values() {
            for quote in arr(entity, "quotes") {
                if names(get(quote, "path"), path) {
                    touch(&pystr(get(entity, "id")), "CITED_BY_SKILL", path, hit(ranges, get(quote, "start"), get(quote, "end")));
                }
            }
        }
        for (key, sources) in &cited {
            for source in sources {
                if names(get(source, "path"), path) {
                    for holder in holders.get(key).into_iter().flatten() {
                        touch(holder, "BINDING_SOURCE_CHANGED", path, hit(ranges, get(source, "start_line"), get(source, "end_line")));
                    }
                }
            }
        }
        for mark in arr(picture, "link_watermarks") {
            let empty = Map::new();
            let recorded = get(mark, "recorded").as_object().unwrap_or(&empty);
            let now = get(mark, "now").as_object().unwrap_or(&empty);
            if recorded.contains_key(*path) || now.contains_key(*path) {
                for holder in holders.get(&(key_str(get(mark, "skill")), key_str(get(mark, "binding")))).into_iter().flatten() {
                    touch(holder, "CONFIRMED_LINK_EVIDENCE_CHANGES", path, Value::Null);
                }
                if let Some(component) = get(mark, "component").as_str() {
                    touch(component, "CONFIRMED_LINK_EVIDENCE_CHANGES", path, Value::Null);
                }
                if recorded.contains_key(*path) {
                    reconfirm.push(json!({"decision_id": get(mark, "id"), "skill": get(mark, "skill"),
                                          "binding": get(mark, "binding"), "component": get(mark, "component"), "path": path}));
                }
            }
        }
    }
    for (skill, binding) in &bindings {
        for holder in holders.get(&(skill.clone(), binding.clone())).into_iter().flatten() {
            touch(holder, "SKILL_BINDING_CHANGED", &format!("skill:{skill}/{binding}"), Value::Null);
        }
    }
    let mut touched: Vec<&String> = touches.keys().collect();
    touched.sort_by(|a, b| {
        let key = |i: &str| (s(get(&ents[i], "layer")) != "business", s(get(&ents[i], "label")).to_owned(), i.to_owned());
        key(a).cmp(&key(b))
    });
    let rels = relationships(bundle, &history(bundle));
    let mut best: BTreeMap<String, ((usize, usize, String), Value)> = BTreeMap::new();
    for origin in &touched {
        for row in impact(origin, &rels, &ents) {
            let id = pystr(get(&row, "id"));
            if touches.contains_key(&id) { continue; }
            let key = (weakest(s(get(&row, "standing"))), int(get(&row, "depth")).unwrap_or(0) as usize, (*origin).clone());
            if best.get(&id).is_none_or(|(k, _)| key < *k) {
                let mut row = row;
                row["via"] = json!(origin);
                best.insert(id, (key, row));
            }
        }
    }
    let mut impacted: Vec<((usize, usize, String), Value)> = best.into_values().collect();
    impacted.sort_by(|a, b| (&a.0, s(get(&a.1, "id"))).cmp(&(&b.0, s(get(&b.1, "id")))));
    reconfirm.sort_by_key(|r| (pystr(get(r, "decision_id")), s(get(r, "path")).to_owned()));
    let touched_rows: Vec<Value> = touched.iter().take(MAX_TOUCHED).map(|i| {
        let mut row = side(&ents, &json!(i)).as_object().cloned().unwrap_or_default();
        row.insert("reasons".into(), Value::Array(touches[*i].values().cloned().collect()));
        Value::Object(row)
    }).collect();
    let total = impacted.len();
    Ok(json!({"schema_version": 1, "kind": "CHANGE_IMPACT", "changed_files": files.len(),
              "touched": touched_rows, "touched_total": touched.len(),
              "impacted": impacted.into_iter().take(MAX_IMPACT).map(|(_, r)| r).collect::<Vec<_>>(),
              "impacted_total": total,
              "reconfirm_after_merge": reconfirm.into_iter().take(MAX_RELATIONSHIPS).collect::<Vec<_>>(),
              "unmapped_files": files.iter().filter(|(p, _)| !mapped.contains(*p)).map(|(p, _)| *p)
                  .take(MAX_RELATIONSHIPS).collect::<Vec<_>>(),
              "semantics": "DECLARED_REACHABILITY_FROM_CHANGED_FILES_NOT_RUNTIME_IMPACT",
              "execution_authorized": false}))
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
    fn a_confirmation_whose_evidence_changed_reads_as_inferred_until_reconfirmed() {
        let mark = |recorded: Value, now: Value| {
            let mut b = bundle();
            b["business"]["link_watermarks"] = json!([{"id": "d1", "skill": "s", "binding": "b.pay",
                                                       "component": "svc:api", "recorded": recorded, "now": now}]);
            let c = context(&b, "s:action:Pay").unwrap();
            c["relationships"].as_array().unwrap().iter().find(|r| r["other"]["id"] == "svc:api").unwrap().clone()
        };
        let stale = mark(json!({"compose.yml": "a"}), json!({"compose.yml": "b"}));
        assert_eq!(stale["standing"], "INFERRED");
        assert_eq!(stale["basis"], "CONFIRMED_BEFORE_EVIDENCE_CHANGED");
        assert_eq!(stale["evidence_check"]["changed"], json!(["compose.yml"]));
        assert_eq!(stale["evidence_check"]["decision_id"], "d1");
        assert_eq!(stale["decision"]["by"], "Ana");
        assert_eq!(mark(json!({"compose.yml": "b"}), json!({"compose.yml": "b"}))["standing"], "CONFIRMED");
        assert_eq!(mark(Value::Null, json!({}))["evidence_check"]["status"], "NOT_RECORDED");
        assert_eq!(watermark_standing(&json!({"f": "0"}), &json!({"f": "0", "g": "1"})), "INFERRED");
    }

    #[test]
    fn a_change_touches_what_its_files_declare_and_reports_the_rest_unmapped() {
        let mut b = bundle();
        b["state"]["nodes"][1]["source_line"] = json!(7);
        b["changes"] = json!({"files": [{"path": "compose.yml", "ranges": [[6, 8]]}, {"path": "README.md"}]});
        let r = change_impact(&b).unwrap();
        let db = r["touched"].as_array().unwrap().iter().find(|t| t["id"] == "svc:db").unwrap();
        assert_eq!(db["reasons"][0]["reason"], "DECLARED_IN_CHANGED_FILE");
        assert_eq!(db["reasons"][0]["line_hit"], true);
        assert_eq!(r["unmapped_files"], json!(["README.md"]));
        assert!(r["impacted"].as_array().unwrap().iter().any(|i| i["id"] == "s:action:Pay"));
        b["changes"] = json!({"files": [{"path": "a"}, {"path": "a"}]});
        assert!(change_impact(&b).is_err());
    }

    #[test]
    fn impact_reaches_the_action_through_a_declared_dependency() {
        let c = context(&bundle(), "svc:db").unwrap();
        let labels: Vec<&str> = c["relied_on_by"].as_array().unwrap().iter().map(|r| r["label"].as_str().unwrap()).collect();
        // api relies on db through a declared dependency. Pay relies on db directly through the
        // undecided suggestion (inferred) and through its confirmed link to api (source): the
        // stronger chain decides, though it is longer.
        assert_eq!(labels, ["api", "Pay"]);
        assert_eq!(c["relied_on_by"][0]["standing"], "SOURCE");
        assert_eq!(c["relied_on_by"][1]["standing"], "SOURCE");
        assert_eq!(c["relied_on_by"][1]["depth"], 2);
        assert_eq!(c["relied_on_by"][1]["path"].as_array().unwrap().len(), 2);
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
