//! Business-layer kernels mirrored from the Python reference implementation:
//! link suggestions, action health rollup, and the evidence grade ladder.
//!
//! Suggestions are proposals explained by name and path signals. Nothing here
//! confirms a link, observes a system, approves a rule, or authorizes execution.
//! Parity with Python is checked in CI; the rollup and ladder are also stated and
//! proven in Lean (proofs/lean) and Bend (proofs/bend).

use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::collections::BTreeSet;

pub const MAX_PER_BINDING: usize = 5;
const MAX_BINDINGS: usize = 4096;
const MAX_COMPONENTS: usize = 4096;
const MAX_PAIRS: usize = 8192;
const MAX_TEXT: usize = 4096;
const KINDS: [&str; 4] = ["declared_service", "source_module", "api_route", "declared_db_table"];
const STRENGTH: [&str; 3] = ["STRONG", "MODERATE", "WEAK"];
const STOPWORDS: &[&str] = &[
    "the", "and", "for", "get", "set", "apply", "type", "class", "def", "func", "function",
    "impl", "src", "lib", "libs", "app", "apps", "main", "index", "init", "core", "common",
    "util", "utils", "module", "modules", "test", "tests", "spec", "handler", "handlers",
    "model", "models", "domain", "code", "py", "js", "ts", "tsx", "jsx", "mjs", "go", "rs",
    "java", "rb", "yml", "yaml", "json", "toml", "compose", "docker", "dockerfile", "package",
];

/// ASCII-only, camelCase-aware word set; the same rules as `link_suggest.tokens`.
pub fn tokens(text: &str) -> Vec<String> {
    let chars: Vec<char> = text.chars().map(|c| if c.is_ascii() { c } else { ' ' }).collect();
    let mut spaced = String::with_capacity(chars.len() * 2);
    for (i, c) in chars.iter().enumerate() {
        if i > 0 && c.is_ascii_uppercase() {
            let p = chars[i - 1];
            if p.is_ascii_lowercase() || p.is_ascii_digit() {
                spaced.push(' ');
            }
        }
        spaced.push(*c);
    }
    let lower = spaced.to_ascii_lowercase();
    let words: BTreeSet<String> = lower
        .split(|c: char| !(c.is_ascii_lowercase() || c.is_ascii_digit()))
        .filter(|w| w.len() >= 2 && !STOPWORDS.contains(w))
        .map(str::to_owned)
        .collect();
    words.into_iter().collect()
}

/// Python `PurePosixPath(name).stem`.
fn stem(name: &str) -> &str {
    match name.rfind('.') {
        Some(i) if i > 0 && i + 1 < name.len() => &name[..i],
        _ => name,
    }
}

/// Directory names and the file stem, each normalized like a label.
pub fn segments(path: &str) -> Vec<String> {
    let parts: Vec<&str> = path.split('/').filter(|p| !p.is_empty() && *p != ".").collect();
    let mut names: Vec<&str> = Vec::new();
    if let Some((last, dirs)) = parts.split_last() {
        names.extend(dirs.iter().copied());
        names.push(stem(last));
    }
    names.into_iter().map(tokens).filter(|t| !t.is_empty()).map(|t| t.join(" ")).collect()
}

fn same_file(a: &str, b: &str) -> bool {
    a == b || a.ends_with(&format!("/{b}")) || b.ends_with(&format!("/{a}"))
}

/// The JSON text Python's `canonical()` produces for a list of strings.
fn canonical_strings(values: &[&str]) -> String {
    let items: Vec<String> = values.iter()
        .map(|v| format!("  {}", serde_json::to_string(v).unwrap_or_default()))
        .collect();
    format!("[\n{}\n]\n", items.join(",\n"))
}

pub fn suggestion_id(skill: &str, binding: &str, component: &str) -> String {
    let hash = Sha256::digest(canonical_strings(&[skill, binding, component]).as_bytes());
    hash.iter().take(12).map(|b| format!("{b:02x}")).collect()
}

fn text<'a>(row: &'a Value, field: &str) -> Result<&'a str, String> {
    let value = row.get(field).and_then(Value::as_str)
        .ok_or_else(|| format!("missing or invalid {field}"))?;
    if value.len() > MAX_TEXT {
        return Err(format!("{field} is too long"));
    }
    Ok(value)
}

fn pairs(data: &Value, field: &str) -> Result<BTreeSet<(String, String, String)>, String> {
    let rows = data.get(field).and_then(Value::as_array)
        .ok_or_else(|| format!("missing {field}"))?;
    if rows.len() > MAX_PAIRS {
        return Err(format!("too many {field} pairs"));
    }
    rows.iter().map(|row| match row.as_array().map(Vec::as_slice) {
        Some([a, b, c]) => match (a.as_str(), b.as_str(), c.as_str()) {
            (Some(a), Some(b), Some(c)) => Ok((a.to_owned(), b.to_owned(), c.to_owned())),
            _ => Err(format!("invalid {field} pair")),
        },
        _ => Err(format!("invalid {field} pair")),
    }).collect()
}

fn signal(name: &str, strength: &str, detail: String) -> Value {
    json!({"signal": name, "strength": strength, "detail": detail})
}

fn rank(strength: &str) -> usize {
    STRENGTH.iter().position(|s| *s == strength).unwrap_or(STRENGTH.len())
}

/// Pure matcher over `link_suggest.prepare` output; byte-for-byte the same result.
pub fn suggest_from(data: &Value) -> Result<Value, String> {
    if data.get("kind").and_then(Value::as_str) != Some("LINK_MATCH_INPUT")
        || data.get("execution_authorized") != Some(&Value::Bool(false)) {
        return Err("expected link match input that authorizes nothing".into());
    }
    let bindings = data.get("bindings").and_then(Value::as_array).ok_or("missing bindings")?;
    let components = data.get("components").and_then(Value::as_array).ok_or("missing components")?;
    if bindings.len() > MAX_BINDINGS || components.len() > MAX_COMPONENTS {
        return Err("match input exceeds its budget".into());
    }
    for c in components {
        if !KINDS.contains(&text(c, "kind")?) {
            return Err("component kind cannot be linked".into());
        }
    }
    let linked = pairs(data, "linked")?;
    let rejected = pairs(data, "rejected")?;
    let mut out: Vec<Value> = Vec::new();
    let mut skipped = 0usize;
    for b in bindings {
        let (skill, binding, binds) = (text(b, "skill")?, text(b, "binding")?, text(b, "binds")?);
        let (system, path) = (text(b, "system")?, text(b, "path")?);
        let b_segments: BTreeSet<String> = segments(path).into_iter().collect();
        let mut b_tokens: BTreeSet<String> = tokens(binding).into_iter()
            .chain(tokens(binds)).chain(tokens(system)).collect();
        for seg in &b_segments {
            b_tokens.extend(seg.split(' ').map(str::to_owned));
        }
        let system_tokens: BTreeSet<String> = tokens(system).into_iter().collect();
        let mut found: Vec<(usize, usize, String, Value)> = Vec::new();
        for c in components {
            let (cid, label, kind, cpath) = (text(c, "id")?, text(c, "label")?, text(c, "kind")?, text(c, "path")?);
            let key = (skill.to_owned(), binding.to_owned(), cid.to_owned());
            if linked.contains(&key) {
                continue;
            }
            let label_words = tokens(label);
            let label_norm = label_words.join(" ");
            let label_tokens: BTreeSet<String> = label_words.into_iter().collect();
            let mut signals = Vec::new();
            if !path.is_empty() && !cpath.is_empty() && kind != "declared_service" && same_file(path, cpath) {
                signals.push(signal("SAME_SOURCE_FILE", "STRONG", format!("{path} is {cpath}")));
            }
            if !label_norm.is_empty() && b_segments.contains(&label_norm) {
                signals.push(signal("PATH_NAMES_COMPONENT", "MODERATE",
                                    format!("{path} has a part named {label}")));
            }
            if !label_tokens.is_empty() && label_tokens.is_subset(&system_tokens) {
                signals.push(signal("SYSTEM_NAMES_COMPONENT", "MODERATE",
                                    format!("system {system} names {label}")));
            }
            let shared: Vec<&str> = label_tokens.intersection(&b_tokens).map(String::as_str).collect();
            if !shared.is_empty() {
                signals.push(signal("SHARED_NAME", "WEAK", format!("shared words: {}", shared.join(", "))));
            }
            if signals.is_empty() {
                continue;
            }
            if rejected.contains(&key) {
                skipped += 1;
                continue;
            }
            let strength = signals.iter()
                .filter_map(|s| s.get("strength").and_then(Value::as_str))
                .min_by_key(|s| rank(s)).unwrap_or("WEAK").to_owned();
            let count = signals.len();
            let row = json!({
                "id": suggestion_id(skill, binding, cid), "skill": skill, "binding": binding,
                "component": cid, "component_label": label, "component_kind": kind,
                "strength": strength, "signals": signals, "status": "PROPOSED",
            });
            found.push((rank(&strength), usize::MAX - count, cid.to_owned(), row));
        }
        found.sort_by(|a, b| (a.0, a.1, &a.2).cmp(&(b.0, b.1, &b.2)));
        out.extend(found.into_iter().take(MAX_PER_BINDING).map(|f| f.3));
    }
    Ok(json!({
        "schema_version": 1, "kind": "LINK_SUGGESTIONS", "suggestions": out,
        "rejected_not_shown": skipped,
        "semantics": "NAME_AND_PATH_SIGNALS_NOT_RUNTIME_EVIDENCE_CONFIRMATION_REQUIRED",
        "auto_applied": 0, "execution_authorized": false,
    }))
}

pub const STATUSES: [&str; 5] = ["ok", "warning", "error", "stale", "unknown"];

/// Most urgent source-reported condition among linked systems (`business_view.rollup`).
pub fn rollup(systems: &[(&str, bool)]) -> &'static str {
    if systems.is_empty() {
        "UNLINKED"
    } else if systems.iter().any(|s| s.0 == "error") {
        "AT_RISK"
    } else if systems.iter().any(|s| s.1) {
        "POSSIBLY_IMPACTED"
    } else if systems.iter().any(|s| s.0 == "warning") {
        "DEGRADED"
    } else if systems.iter().any(|s| s.0 == "stale") {
        "STALE"
    } else if systems.iter().any(|s| s.0 != "ok") {
        "UNKNOWN"
    } else {
        "REPORTED_OK"
    }
}

/// The evidence grade ladder (`evidence_grade.ladder`): grade and stopping reason.
pub fn ladder(rejected: bool, supported: bool, observed: bool, multi_kind: bool,
              failed: bool, passed: bool) -> (&'static str, &'static str) {
    if rejected { return ("G0", "REJECTED"); }
    if !supported { return ("G0", "NOT_REVIEWED"); }
    if !observed { return ("G1", "INFERRED"); }
    if !multi_kind { return ("G1", "ONE_KIND"); }
    if failed { return ("G2", "CHECK_FAILED"); }
    if !passed { return ("G2", "NOT_EXERCISED"); }
    ("G3", "NO_G4_PRODUCER")
}

/// Every system list of length 0..=3 over the 10 (status, impacted) values, in
/// the order shared with Python and Lean (tests/conformance.py).
pub fn conformance_table() -> Value {
    let values: Vec<(&str, bool)> = STATUSES.iter()
        .flat_map(|s| [(*s, false), (*s, true)]).collect();
    let mut sequences: Vec<Vec<usize>> = vec![vec![]];
    let mut frontier: Vec<Vec<usize>> = vec![vec![]];
    for _ in 0..3 {
        let mut next = Vec::new();
        for prefix in &frontier {
            for i in 0..values.len() {
                let mut seq = prefix.clone();
                seq.push(i);
                next.push(seq);
            }
        }
        sequences.extend(next.iter().cloned());
        frontier = next;
    }
    let rollups: Vec<Value> = sequences.iter().map(|seq| {
        let systems: Vec<(&str, bool)> = seq.iter().map(|i| values[*i]).collect();
        json!([seq, rollup(&systems)])
    }).collect();
    let mut grades = Vec::new();
    for bits in 0..64u32 {
        let flag: Vec<bool> = (0..6).map(|i| bits & (1 << (5 - i)) != 0).collect();
        let (grade, reason) = ladder(flag[0], flag[1], flag[2], flag[3], flag[4], flag[5]);
        let as_ints: Vec<u8> = flag.iter().map(|f| u8::from(*f)).collect();
        grades.push(json!([as_ints, grade, reason]));
    }
    json!({"kind": "BUSINESS_CONFORMANCE_TABLE", "rollup": rollups, "grade": grades,
           "fusion": fusion_rows(), "execution_authorized": false})
}

/// Source combining through the reference core: each sample comes from its own
/// source, fresh or expired, in the order shared with tests/conformance.py.
fn fusion_rows() -> Vec<Value> {
    use crate::{project, DisplayHealth, Node, Observation, ReportedHealth, Topology};
    let samples = [(ReportedHealth::Healthy, false), (ReportedHealth::Healthy, true),
                   (ReportedHealth::Degraded, false), (ReportedHealth::Degraded, true),
                   (ReportedHealth::Failed, false), (ReportedHealth::Failed, true)];
    let sources = ["otel", "health_probe", "prometheus"];
    let topology = Topology {
        schema_version: 1, root_goal_id: "conformance".into(), version_id: "v1".into(),
        nodes: vec![Node { id: "db".into(), kind: "declared_service".into(),
                           label: "db".into(), evidence_id: "E-db".into() }],
        edges: vec![],
    };
    let mut sequences: Vec<Vec<usize>> = vec![vec![]];
    let mut frontier: Vec<Vec<usize>> = vec![vec![]];
    for _ in 0..3 {
        let next: Vec<Vec<usize>> = frontier.iter().flat_map(|prefix| (0..samples.len()).map(move |i| {
            let mut seq = prefix.clone();
            seq.push(i);
            seq
        })).collect();
        sequences.extend(next.iter().cloned());
        frontier = next;
    }
    sequences.iter().map(|seq| {
        let rows: Vec<Observation> = seq.iter().zip(sources).map(|(i, source)| {
            let (health, fresh) = samples[*i];
            Observation { component_id: "db".into(), source_id: source.into(),
                          observed_at_ms: if fresh { 10_000 } else { 0 }, ttl_ms: 5_000,
                          reported_health: health }
        }).collect();
        let shown = match project(&topology, &rows, 10_000).map(|p| p.nodes[0].health) {
            Ok(DisplayHealth::Healthy) => "ok", Ok(DisplayHealth::Degraded) => "warning",
            Ok(DisplayHealth::Failed) => "error", Ok(DisplayHealth::Stale) => "stale",
            Ok(DisplayHealth::Unknown) => "unknown", Ok(DisplayHealth::Conflicting) => "conflict",
            Err(_) => "invalid",
        };
        json!([seq, shown])
    }).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tokens_match_python_rules() {
        assert_eq!(tokens("applyRefundToPayment"), ["payment", "refund", "to"]);
        assert_eq!(tokens("payments.apply_refund"), ["payments", "refund"]);
        assert_eq!(tokens("Zahlungsdienst-é-x"), ["zahlungsdienst"]);
        assert!(tokens("src/main.py").is_empty());
        assert_eq!(segments("services/payments/refund.py"), ["services", "payments", "refund"]);
        assert_eq!(stem(".env"), ".env");
        assert_eq!(stem("a.tar.gz"), "a.tar");
    }

    #[test]
    fn suggestion_id_is_python_canonical_sha256_prefix() {
        // sha256 of '[\n  "s",\n  "b",\n  "c"\n]\n', first 24 hex digits.
        assert_eq!(suggestion_id("s", "b", "c").len(), 24);
        assert_eq!(canonical_strings(&["s", "b", "c"]), "[\n  \"s\",\n  \"b\",\n  \"c\"\n]\n");
    }

    #[test]
    fn rollup_safety() {
        assert_eq!(rollup(&[]), "UNLINKED");
        assert_eq!(rollup(&[("ok", false), ("error", false)]), "AT_RISK");
        assert_eq!(rollup(&[("ok", false), ("unknown", false)]), "UNKNOWN");
        assert_eq!(rollup(&[("ok", true)]), "POSSIBLY_IMPACTED");
        assert_eq!(rollup(&[("ok", false), ("ok", false)]), "REPORTED_OK");
    }

    #[test]
    fn ladder_never_reaches_g4_and_rejection_wins() {
        for bits in 0..64u32 {
            let f: Vec<bool> = (0..6).map(|i| bits & (1 << i) != 0).collect();
            let (grade, _) = ladder(f[0], f[1], f[2], f[3], f[4], f[5]);
            assert_ne!(grade, "G4");
            if f[0] { assert_eq!(grade, "G0"); }
            if !f[2] { assert!(grade == "G0" || grade == "G1"); }
        }
    }

    #[test]
    fn suggestions_are_proposals_only() {
        let data = json!({"kind": "LINK_MATCH_INPUT", "execution_authorized": false,
            "bindings": [{"skill": "s", "binding": "billing.charge", "binds": "Charge",
                          "system": "", "path": "billing/charge.py"}],
            "components": [{"id": "a", "label": "billing", "kind": "declared_service", "path": "compose.yml"}],
            "linked": [], "rejected": []});
        let out = suggest_from(&data).unwrap();
        assert_eq!(out["auto_applied"], 0);
        assert_eq!(out["suggestions"][0]["status"], "PROPOSED");
        assert_eq!(out["suggestions"][0]["strength"], "MODERATE");
        let mut authorizing = data.clone();
        authorizing["execution_authorized"] = json!(true);
        assert!(suggest_from(&authorizing).is_err());
    }
}
