//! Integration tests for the decision kernel, through the library and through the
//! `ontologyex-view decide` binary the control room runs. The same laws are proved about a
//! Lean model (proofs/lean/OntologyEXSpec/Decisions.lean); these check the shipped code.
use ontologyex_core::decide::{commit, version};
use serde_json::{json, Value};
use std::io::Write;
use std::process::{Command, Stdio};

fn entry(list: &str, v: i64) -> Value {
    json!({"list": list, "row": {"v": v, "declared_by": "Dana"}})
}

fn entries(items: &[(&str, &str, i64)]) -> Value {
    Value::Array(items.iter().map(|(id, list, v)| json!({"id": id, "list": list, "row": {"v": v, "declared_by": "Dana"}})).collect())
}

fn op(id: &str, expect: &Value, set: Value) -> Value {
    json!({"id": id, "expect": version(expect), "set": set})
}

/// The entries as a map from ID to entry, ignoring order.
fn state(out: &Value) -> std::collections::BTreeMap<String, Value> {
    out["entries"].as_array().unwrap().iter()
        .map(|e| (e["id"].as_str().unwrap().to_owned(), json!({"list": e["list"], "row": e["row"]}))).collect()
}

/// The batch that undoes an applied one, from the changes it recorded (as decisions.undo_batch_ops does).
fn undo_of(applied: &Value) -> Value {
    Value::Array(applied["changes"].as_array().unwrap().iter()
        .map(|c| json!({"id": c["id"], "expect": version(&c["after"]), "set": c["before"]})).collect())
}

#[test]
fn a_stale_approval_is_refused_and_nothing_changes() {
    let before = entries(&[("a", "confirmed", 1), ("b", "confirmed", 2)]);
    // The person saw b at version v=2; someone re-confirmed it as v=3 since.
    let now = entries(&[("a", "confirmed", 1), ("b", "confirmed", 3)]);
    let batch = json!([op("a", &entry("confirmed", 1), Value::Null), op("b", &entry("confirmed", 2), Value::Null)]);
    assert!(commit(&before, &batch).unwrap().get("changes").is_some());
    let refused = commit(&now, &batch).unwrap();
    assert_eq!(refused, json!({"refused": "STALE", "ids": ["b"]}));  // a is current, but nothing applies
}

#[test]
fn a_batch_applies_whole_and_touches_nothing_else() {
    let before = entries(&[("a", "confirmed", 1), ("b", "rejected", 2), ("c", "confirmed", 3)]);
    let batch = json!([
        op("a", &entry("confirmed", 1), Value::Null),                 // undo
        op("d", &Value::Null, entry("confirmed", 4)),                  // confirm
        op("b", &entry("rejected", 2), entry("confirmed", 5)),          // moved to confirmed
    ]);
    let out = commit(&before, &batch).unwrap();
    let s = state(&out);
    assert!(!s.contains_key("a"));
    assert_eq!(s["d"], entry("confirmed", 4));
    assert_eq!(s["b"], entry("confirmed", 5));
    assert_eq!(s["c"], entry("confirmed", 3));  // untouched
    assert_eq!(out["changes"].as_array().unwrap().len(), 3);
    // file order: c keeps its place; new and moved entries go last, in batch order
    let ids: Vec<&str> = out["entries"].as_array().unwrap().iter().map(|e| e["id"].as_str().unwrap()).collect();
    assert_eq!(ids, ["c", "d", "b"]);
}

#[test]
fn an_item_named_twice_is_refused() {
    let batch = json!([op("a", &Value::Null, entry("confirmed", 1)), op("a", &Value::Null, entry("rejected", 1))]);
    assert_eq!(commit(&json!([]), &batch).unwrap(), json!({"refused": "DUPLICATE_ITEM", "ids": ["a"]}));
}

#[test]
fn undo_puts_back_exactly_what_the_batch_changed() {
    let before = entries(&[("a", "confirmed", 1), ("b", "confirmed", 2)]);
    let batch = json!([op("a", &entry("confirmed", 1), entry("confirmed", 9)), op("b", &entry("confirmed", 2), Value::Null),
                       op("c", &Value::Null, entry("rejected", 3))]);
    let applied = commit(&before, &batch).unwrap();
    let undone = commit(&applied["entries"], &undo_of(&applied)).unwrap();
    assert_eq!(state(&undone), state(&json!({"entries": before})));
}

#[test]
fn undo_keeps_later_decisions_on_other_items() {
    let before = entries(&[("a", "confirmed", 1)]);
    let applied = commit(&before, &json!([op("a", &entry("confirmed", 1), Value::Null),
                                          op("b", &Value::Null, entry("confirmed", 2))])).unwrap();
    // Later, someone else decides c and d.
    let later = commit(&applied["entries"], &json!([op("c", &Value::Null, entry("rejected", 3)),
                                                    op("d", &Value::Null, entry("confirmed", 4))])).unwrap();
    let undone = commit(&later["entries"], &undo_of(&applied)).unwrap();
    let s = state(&undone);
    assert_eq!(s["a"], entry("confirmed", 1));  // put back
    assert!(!s.contains_key("b"));              // taken back
    assert_eq!(s["c"], entry("rejected", 3));   // later decisions kept
    assert_eq!(s["d"], entry("confirmed", 4));
}

#[test]
fn undo_is_refused_when_a_later_decision_changed_one_of_its_items() {
    let applied = commit(&json!([]), &json!([op("a", &Value::Null, entry("confirmed", 1)),
                                             op("b", &Value::Null, entry("confirmed", 2))])).unwrap();
    let later = commit(&applied["entries"], &json!([op("b", &entry("confirmed", 2), entry("confirmed", 7))])).unwrap();
    let refused = commit(&later["entries"], &undo_of(&applied)).unwrap();
    assert_eq!(refused, json!({"refused": "STALE", "ids": ["b"]}));
}

#[test]
fn batches_on_different_items_commute() {
    // A small deterministic generator: the same in every run.
    let mut seed: u64 = 20261029;
    let mut draw = |m: u64| { seed = (seed.wrapping_mul(1103515245).wrapping_add(12345)) % 2147483648; (seed / 65536) % m };
    let mut checked = 0;
    for _ in 0..500 {
        let items: Vec<String> = (0..6).map(|i| format!("k{i}")).collect();
        let start: Vec<(String, Value)> = items.iter().filter_map(|k| {
            let v = draw(5) as i64;
            (v < 4).then(|| (k.clone(), entry(if v % 2 == 0 { "confirmed" } else { "rejected" }, v)))
        }).collect();
        let base = Value::Array(start.iter().map(|(k, e)| json!({"id": k, "list": e["list"], "row": e["row"]})).collect());
        let now = |k: &str| start.iter().find(|(id, _)| id == k).map(|(_, e)| e.clone()).unwrap_or(Value::Null);
        let mk = |ks: &[usize], draw: &mut dyn FnMut(u64) -> u64| -> Value {
            Value::Array(ks.iter().map(|&i| {
                let k = &items[i];
                let set = match draw(5) { 4 => Value::Null, v => entry(if v % 2 == 0 { "confirmed" } else { "rejected" }, v as i64 + 10) };
                let expect = if draw(6) == 0 { entry("confirmed", 99) } else { now(k) };
                op(k, &expect, set)
            }).collect())
        };
        let b1 = mk(&[0, 1, 2][..1 + draw(3) as usize], &mut draw);
        let b2 = mk(&[3, 4, 5][..1 + draw(3) as usize], &mut draw);
        let then = |first: &Value, second: &Value| -> Option<std::collections::BTreeMap<String, Value>> {
            let one = commit(&base, first).unwrap();
            if one.get("refused").is_some() { return None; }
            let two = commit(&one["entries"], second).unwrap();
            if two.get("refused").is_some() { None } else { Some(state(&two)) }
        };
        assert_eq!(then(&b1, &b2), then(&b2, &b1));
        checked += 1;
    }
    assert_eq!(checked, 500);
}

#[test]
fn the_binary_decides_and_refuses_an_authorizing_input() {
    let run = |input: Value| {
        let mut child = Command::new(env!("CARGO_BIN_EXE_ontologyex-view")).arg("decide")
            .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::null()).spawn().unwrap();
        child.stdin.take().unwrap().write_all(input.to_string().as_bytes()).unwrap();
        let out = child.wait_with_output().unwrap();
        (out.status.success(), serde_json::from_slice::<Value>(&out.stdout).unwrap_or(Value::Null))
    };
    let batch = json!([op("a", &Value::Null, entry("confirmed", 1))]);
    let (ok, answer) = run(json!({"execution_authorized": false, "entries": [], "ops": batch}));
    assert!(ok);
    assert_eq!(answer["kind"], "DECISION_COMMIT");
    assert_eq!(answer["execution_authorized"], false);
    assert_eq!(answer["entries"][0]["id"], "a");
    let (ok, _) = run(json!({"execution_authorized": true, "entries": [], "ops": batch}));
    assert!(!ok);
    let (ok, _) = run(json!({"execution_authorized": false, "entries": [], "ops": []}));
    assert!(!ok);  // an empty batch is not a decision
}
