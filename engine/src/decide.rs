//! The decision kernel: a batch of compare-and-set operations on decided rows, applied whole
//! or not at all. Same function as `decisions.commit` in Python (tests/rust_decide_parity.py
//! compares them); the laws it keeps are stated in proofs/lean/OntologyEXSpec/Decisions.lean
//! and proved about a Lean model of it. Pure: JSON in, JSON out, no file access. The control
//! room reads and writes the decisions file around it (with --view-engine rust).
//!
//! An operation is `{"id", "expect": VERSION | null, "set": {"list", "row"} | null}`: the item
//! must be at version `expect` (null: not decided) and becomes `set` (null: not decided). A
//! version is the first 24 hex digits of the sha256 of Python's canonical JSON of the entry.
use crate::pyjson;
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use std::collections::{BTreeSet, HashMap, HashSet};

pub const MAX_BATCH: usize = 100;
const LISTS: [&str; 2] = ["confirmed", "rejected"];

/// The version of a decided entry ({"list", "row"}); None when the item is not decided.
pub fn version(entry: &Value) -> Option<String> {
    if entry.is_null() {
        return None;
    }
    let canonical = pyjson::dumps(&json!({"list": entry["list"], "row": entry["row"]}), Some(2), false) + "\n";
    let hash = Sha256::digest(canonical.as_bytes());
    Some(hash.iter().map(|b| format!("{b:02x}")).collect::<String>()[..24].to_owned())
}

fn is_entry(v: &Value) -> bool {
    v.as_object().is_some_and(|o| o.len() == 2 && o.get("list").and_then(Value::as_str).is_some_and(|l| LISTS.contains(&l))
        && o.get("row").is_some_and(Value::is_object))
}

fn keys_are(o: &Map<String, Value>, keys: &[&str]) -> bool {
    o.len() == keys.len() && keys.iter().all(|k| o.contains_key(*k))
}

/// What applying `ops` to `entries` gives: `Ok({"entries", "changes"})`, or `Ok({"refused", "ids"})`
/// when an item was decided twice in the batch (DUPLICATE_ITEM) or is no longer at the version
/// expected (STALE), in which case nothing changes. `Err` for malformed input.
pub fn commit(entries: &Value, ops: &Value) -> Result<Value, String> {
    let entries = entries.as_array().ok_or("invalid decided entries")?;
    for e in entries {
        let o = e.as_object().ok_or("invalid decided entries")?;
        if !keys_are(o, &["id", "list", "row"]) || !o["id"].is_string()
            || !is_entry(&json!({"list": o["list"], "row": o["row"]})) {
            return Err("invalid decided entries".into());
        }
    }
    let ids: HashSet<&str> = entries.iter().map(|e| e["id"].as_str().unwrap_or("")).collect();
    if ids.len() != entries.len() {
        return Err("an item is decided twice".into());
    }
    let ops = ops.as_array().ok_or("a batch is a list of operations")?;
    if ops.is_empty() || ops.len() > MAX_BATCH {
        return Err(format!("a batch is 1 to {MAX_BATCH} operations"));
    }
    for op in ops {
        let o = op.as_object().ok_or("invalid operation")?;
        if !keys_are(o, &["id", "expect", "set"]) || !o["id"].is_string()
            || !(o["expect"].is_null() || o["expect"].is_string()) || !(o["set"].is_null() || is_entry(&o["set"])) {
            return Err("invalid operation".into());
        }
    }
    let op_ids: Vec<&str> = ops.iter().map(|op| op["id"].as_str().unwrap_or("")).collect();
    let mut seen = HashSet::new();
    let twice: BTreeSet<&str> = op_ids.iter().copied().filter(|i| !seen.insert(*i)).collect();
    if !twice.is_empty() {
        return Ok(json!({"refused": "DUPLICATE_ITEM", "ids": twice.into_iter().collect::<Vec<_>>()}));
    }
    let current: HashMap<&str, Value> = entries.iter()
        .map(|e| (e["id"].as_str().unwrap_or(""), json!({"list": e["list"], "row": e["row"]}))).collect();
    let now = |id: &str| current.get(id).cloned().unwrap_or(Value::Null);
    let stale: Vec<&str> = ops.iter().zip(&op_ids)
        .filter(|(op, id)| version(&now(id)).as_deref() != op["expect"].as_str())
        .map(|(_, id)| *id).collect();
    if !stale.is_empty() {
        return Ok(json!({"refused": "STALE", "ids": stale}));
    }
    let mut out: Vec<Value> = entries.clone();
    let mut changes = Vec::new();
    for (op, id) in ops.iter().zip(&op_ids) {
        let (before, after) = (now(id), op["set"].clone());
        if version(&before) == version(&after) {
            continue;
        }
        changes.push(json!({"id": id, "before": before, "after": after}));
        let at = out.iter().position(|e| e["id"] == *id);
        if let (false, Some(i)) = (after.is_null(), at) {
            if out[i]["list"] == after["list"] {
                out[i] = json!({"id": id, "list": after["list"], "row": after["row"]});
                continue;
            }
        }
        if let Some(i) = at {
            out.remove(i);
        }
        if !after.is_null() {
            out.push(json!({"id": id, "list": after["list"], "row": after["row"]}));
        }
    }
    Ok(json!({"entries": out, "changes": changes}))
}

/// `ontologyex-view decide`: `{"execution_authorized": false, "entries", "ops"}` on stdin.
pub fn run(input: &Value) -> Result<Value, String> {
    if input.get("execution_authorized") != Some(&json!(false)) {
        return Err("the input must not authorize execution".into());
    }
    let mut answer = commit(&input["entries"], &input["ops"])?;
    let o = answer.as_object_mut().ok_or("unreachable")?;
    o.insert("kind".into(), json!("DECISION_COMMIT"));
    o.insert("execution_authorized".into(), json!(false));
    Ok(answer)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn version_is_the_python_canonical_sha256_prefix() {
        // decisions.version in Python gives the same digits for this entry.
        let entry = json!({"list": "confirmed", "row": {"skill": "pay", "binding": "Payment", "component": "svc:api",
                                                      "declared_by": "Dana", "note": "é ok", "weight": 0.5}});
        assert_eq!(version(&entry).unwrap(), "5c9747043c274314978b091a");
        assert_eq!(version(&Value::Null), None);
    }
}
