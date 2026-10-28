//! Offline, read-only manifest discovery: a port of `discover_repo.py` whose
//! JSON output is byte-identical (checked in CI by tests/rust_discover_parity.py).
//!
//! It walks the repository like Python's `os.walk` (sorted, no symlinks, same
//! budgets), reads only manifests, and records declarations: projects, compose
//! services and their `depends_on`, workflows, and other manifests. No code is
//! executed and nothing is fetched.
//!
//! Known difference: TOML values or dotted keys nested 80 or more levels deep
//! are a parse gap here (the TOML parser's recursion limit) but parse in Python.

use crate::pyjson;
use crate::pyyaml::{self, Yaml};
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::fs;
use std::os::unix::ffi::OsStrExt;
use std::path::{Component, Path, PathBuf};

pub const EXCLUDED_DIRS: [&str; 13] = [
    ".git", ".venv", "venv", "node_modules", "vendor", "dist", "build",
    "target", ".next", ".turbo", ".cache", "__pycache__", "coverage",
];
pub const MANIFESTS: [&str; 9] = [
    "package.json", "pyproject.toml", "Cargo.toml", "go.mod",
    "compose.yaml", "compose.yml", "docker-compose.yml", "docker-compose.yaml", "Dockerfile",
];
pub const COMPOSE_FILES: [&str; 4] = ["compose.yaml", "compose.yml", "docker-compose.yml", "docker-compose.yaml"];
pub const MAX_WALK_FILES: usize = 6000;
pub const MAX_MANIFESTS: usize = 128;
pub const MAX_FILE_BYTES: u64 = 256 * 1024;
pub const MAX_TOTAL_BYTES: u64 = 2 * 1024 * 1024;
pub const MAX_DEPTH: usize = 6;
pub const MAX_JSON_NESTING: usize = 512;

/// Python's sort key for a file name decoded with `surrogateescape`.
fn py_key(name: &[u8]) -> Vec<u32> {
    let mut out = Vec::with_capacity(name.len());
    let mut rest = name;
    while !rest.is_empty() {
        match std::str::from_utf8(rest) {
            Ok(text) => {
                out.extend(text.chars().map(|c| c as u32));
                break;
            }
            Err(e) => {
                let good = e.valid_up_to();
                out.extend(std::str::from_utf8(&rest[..good]).unwrap().chars().map(|c| c as u32));
                let bad = e.error_len().unwrap_or(rest.len() - good);
                out.extend(rest[good..good + bad].iter().map(|&b| 0xDC00 + b as u32));
                rest = &rest[good + bad..];
            }
        }
    }
    out
}

/// `PurePath.suffix`.
fn suffix(name: &str) -> &str {
    match name.rfind('.') {
        Some(i) if i > 0 && i < name.len() - 1 => &name[i..],
        _ => "",
    }
}

/// `PurePath.stem`.
fn stem(name: &str) -> &str {
    match name.rfind('.') {
        Some(i) if i > 0 && i < name.len() - 1 => &name[..i],
        _ => name,
    }
}

fn file_name(rel: &str) -> &str {
    rel.rsplit('/').next().unwrap_or(rel)
}

/// `discover_repo.eligible` on a POSIX relative path.
pub fn eligible(rel: &str) -> bool {
    let name = file_name(rel);
    if MANIFESTS.contains(&name) {
        return true;
    }
    let lower = suffix(name).to_ascii_lowercase();
    (lower == ".yml" || lower == ".yaml") && rel.starts_with(".github/workflows/")
}

/// `urllib.parse.quote(text, safe=...)`.
fn quote(text: &str, safe: &str) -> String {
    let mut out = String::new();
    for &b in text.as_bytes() {
        let c = b as char;
        if c.is_ascii_alphanumeric() || "_.-~".contains(c) || (b < 128 && safe.contains(c)) {
            out.push(c);
        } else {
            out.push_str(&format!("%{:02X}", b));
        }
    }
    out
}

pub fn canonical_id(kind: &str, path: &str, name: &str) -> String {
    format!("discovery:{}:{}:{}", kind, quote(path, "/-._~"), quote(name, "-._~"))
}

struct Walk {
    matched: Vec<String>,
    visited: usize,
    skipped_links: usize,
}

fn is_symlink(path: &Path) -> bool {
    fs::symlink_metadata(path).map(|m| m.file_type().is_symlink()).unwrap_or(false)
}

/// `discover_repo.candidates`: a sorted, top-down walk that never follows links.
fn candidates(repo: &Path) -> Result<Walk, String> {
    let mut walk = Walk { matched: Vec::new(), visited: 0, skipped_links: 0 };
    // (absolute directory, relative parts, every part valid UTF-8)
    let mut stack: Vec<(PathBuf, Vec<String>, bool)> = vec![(repo.to_path_buf(), Vec::new(), true)];
    while let Some((dir, parts, utf8)) = stack.pop() {
        let Ok(entries) = fs::read_dir(&dir) else { continue };
        let mut dirs: Vec<(Vec<u32>, std::ffi::OsString)> = Vec::new();
        let mut files: Vec<(Vec<u32>, std::ffi::OsString)> = Vec::new();
        for entry in entries {
            let Ok(entry) = entry else { break };
            let name = entry.file_name();
            // os.walk classifies with is_dir(), which follows symlinks.
            let is_dir = fs::metadata(entry.path()).map(|m| m.is_dir()).unwrap_or(false);
            let key = py_key(name.as_bytes());
            if is_dir { dirs.push((key, name)) } else { files.push((key, name)) }
        }
        dirs.sort();
        files.sort();
        let depth = parts.len();
        let mut retained = Vec::new();
        for (_, name) in &dirs {
            let path = dir.join(name);
            let text = name.to_string_lossy();
            if is_symlink(&path) {
                walk.skipped_links += 1;
            } else if ((!EXCLUDED_DIRS.contains(&text.as_ref()) && !text.starts_with('.')) || text == ".github")
                && depth < MAX_DEPTH
            {
                retained.push(name.clone());
            }
        }
        for (_, name) in &files {
            walk.visited += 1;
            if walk.visited > MAX_WALK_FILES {
                return Err("repository walk exceeds file budget".into());
            }
            let path = dir.join(name);
            if is_symlink(&path) {
                walk.skipped_links += 1;
                continue;
            }
            let regular = fs::metadata(&path).map(|m| m.is_file()).unwrap_or(false);
            if !regular {
                continue;
            }
            let Some(name_text) = name.to_str() else {
                if eligible(&name.to_string_lossy()) {
                    return Err("manifest path is not valid UTF-8".into());
                }
                continue;
            };
            let mut rel_parts = parts.clone();
            rel_parts.push(name_text.to_string());
            let rel = rel_parts.join("/");
            if eligible(&rel) {
                if !utf8 {
                    return Err("manifest path is not valid UTF-8".into());
                }
                walk.matched.push(rel);
                if walk.matched.len() > MAX_MANIFESTS {
                    return Err("too many candidate manifests; scope the repository".into());
                }
            }
        }
        for name in retained.into_iter().rev() {
            let mut child = parts.clone();
            child.push(name.to_string_lossy().into_owned());
            stack.push((dir.join(&name), child, utf8 && name.to_str().is_some()));
        }
    }
    walk.matched.sort();
    Ok(walk)
}

// ---------------------------------------------------------------- Python json.loads acceptance

struct JsonParser<'a> {
    s: &'a [u8],
    i: usize,
    depth: usize,
}

impl JsonParser<'_> {
    fn ws(&mut self) {
        while self.i < self.s.len() && matches!(self.s[self.i], b' ' | b'\t' | b'\n' | b'\r') {
            self.i += 1;
        }
    }
    fn eat(&mut self, lit: &str) -> bool {
        if self.s[self.i..].starts_with(lit.as_bytes()) {
            self.i += lit.len();
            true
        } else {
            false
        }
    }
    fn digits(&mut self) -> usize {
        let start = self.i;
        while self.i < self.s.len() && self.s[self.i].is_ascii_digit() {
            self.i += 1;
        }
        self.i - start
    }
    fn number(&mut self) -> Result<Yaml, String> {
        self.eat("-");
        match self.s.get(self.i) {
            Some(b'0') => self.i += 1,
            Some(b'1'..=b'9') => {
                self.digits();
            }
            _ => return Err("Expecting value".into()),
        }
        if self.s.get(self.i) == Some(&b'.') && self.s.get(self.i + 1).is_some_and(u8::is_ascii_digit) {
            self.i += 1;
            self.digits();
        }
        if matches!(self.s.get(self.i), Some(b'e' | b'E')) {
            let save = self.i;
            self.i += 1;
            if matches!(self.s.get(self.i), Some(b'+' | b'-')) {
                self.i += 1;
            }
            if self.digits() == 0 {
                self.i = save;
            }
        }
        Ok(Yaml::Other)
    }
    fn string(&mut self) -> Result<String, String> {
        self.i += 1; // opening quote
        let mut units: Vec<u16> = Vec::new();
        let mut out = String::new();
        let flush = |units: &mut Vec<u16>, out: &mut String| {
            out.push_str(&String::from_utf16_lossy(units));
            units.clear();
        };
        loop {
            let Some(&b) = self.s.get(self.i) else { return Err("Unterminated string".into()) };
            match b {
                b'"' => {
                    self.i += 1;
                    flush(&mut units, &mut out);
                    return Ok(out);
                }
                b'\\' => {
                    let Some(&e) = self.s.get(self.i + 1) else { return Err("Unterminated string".into()) };
                    self.i += 2;
                    let c = match e {
                        b'"' => '"', b'\\' => '\\', b'/' => '/', b'b' => '\u{8}', b'f' => '\u{c}',
                        b'n' => '\n', b'r' => '\r', b't' => '\t',
                        b'u' => {
                            let hex = self.s.get(self.i..self.i + 4).ok_or("Invalid \\uXXXX escape")?;
                            let text = std::str::from_utf8(hex).map_err(|_| "Invalid \\uXXXX escape")?;
                            if !text.bytes().all(|h| h.is_ascii_hexdigit()) {
                                return Err("Invalid \\uXXXX escape".into());
                            }
                            units.push(u16::from_str_radix(text, 16).unwrap());
                            self.i += 4;
                            continue;
                        }
                        _ => return Err("Invalid \\escape".into()),
                    };
                    flush(&mut units, &mut out);
                    out.push(c);
                }
                b if b < 0x20 => return Err("Invalid control character".into()),
                _ => {
                    flush(&mut units, &mut out);
                    // Copy one UTF-8 character (input is valid UTF-8).
                    let len = match b { 0x00..=0x7F => 1, 0xC0..=0xDF => 2, 0xE0..=0xEF => 3, _ => 4 };
                    out.push_str(std::str::from_utf8(&self.s[self.i..self.i + len]).unwrap());
                    self.i += len;
                }
            }
        }
    }
    fn value(&mut self) -> Result<Yaml, String> {
        match self.s.get(self.i) {
            Some(b'"') => self.string().map(Yaml::Str),
            Some(b'{') => {
                self.enter()?;
                let mut entries: Vec<(String, Yaml)> = Vec::new();
                self.ws();
                if self.eat("}") {
                    self.depth -= 1;
                    return Ok(Yaml::Map(entries));
                }
                loop {
                    if self.s.get(self.i) != Some(&b'"') {
                        return Err("Expecting property name enclosed in double quotes".into());
                    }
                    let key = self.string()?;
                    self.ws();
                    if !self.eat(":") {
                        return Err("Expecting ':' delimiter".into());
                    }
                    self.ws();
                    let value = self.value()?;
                    // dict semantics: a repeated key keeps its first position, last value.
                    match entries.iter_mut().find(|(k, _)| *k == key) {
                        Some(slot) => slot.1 = value,
                        None => entries.push((key, value)),
                    }
                    self.ws();
                    if self.eat("}") {
                        break;
                    }
                    if !self.eat(",") {
                        return Err("Expecting ',' delimiter".into());
                    }
                    self.ws();
                }
                self.depth -= 1;
                Ok(Yaml::Map(entries))
            }
            Some(b'[') => {
                self.enter()?;
                let mut items = Vec::new();
                self.ws();
                if self.eat("]") {
                    self.depth -= 1;
                    return Ok(Yaml::Seq(items));
                }
                loop {
                    items.push(self.value()?);
                    self.ws();
                    if self.eat("]") {
                        break;
                    }
                    if !self.eat(",") {
                        return Err("Expecting ',' delimiter".into());
                    }
                    self.ws();
                }
                self.depth -= 1;
                Ok(Yaml::Seq(items))
            }
            Some(b'n') if self.eat("null") => Ok(Yaml::Null),
            Some(b't') if self.eat("true") => Ok(Yaml::Bool(true)),
            Some(b'f') if self.eat("false") => Ok(Yaml::Bool(false)),
            Some(b'N') if self.eat("NaN") => Ok(Yaml::Other),
            Some(b'I') if self.eat("Infinity") => Ok(Yaml::Other),
            Some(b'-') if self.eat("-Infinity") => Ok(Yaml::Other),
            Some(b'-' | b'0'..=b'9') => self.number(),
            _ => Err("Expecting value".into()),
        }
    }
    fn enter(&mut self) -> Result<(), String> {
        self.i += 1;
        self.depth += 1;
        if self.depth > MAX_JSON_NESTING {
            return Err("JSON nesting exceeds limit".into());
        }
        Ok(())
    }
}

/// `json.loads(text)` acceptance (NaN, Infinity, lone surrogate escapes, duplicate keys).
pub fn json_loads(text: &str) -> Result<Yaml, String> {
    let mut p = JsonParser { s: text.as_bytes(), i: 0, depth: 0 };
    p.ws();
    let value = p.value()?;
    p.ws();
    if p.i != p.s.len() {
        return Err("Extra data".into());
    }
    Ok(value)
}

fn toml_doc(text: &str) -> Result<Yaml, String> {
    if text.starts_with('\u{FEFF}') {
        return Err("invalid statement".into());
    }
    let table: toml::Table = text.parse().map_err(|e: toml::de::Error| e.message().to_string())?;
    let name = match table.get("name") {
        Some(toml::Value::String(s)) => vec![("name".to_string(), Yaml::Str(s.clone()))],
        Some(_) => vec![("name".to_string(), Yaml::Other)],
        None => vec![],
    };
    Ok(Yaml::Map(name))
}

/// `discover_repo.parse_manifest`: the document, or the gap category.
fn parse_manifest(rel: &str, data: &[u8]) -> Result<Option<Yaml>, &'static str> {
    let name = file_name(rel);
    if rel.ends_with(".json") || rel.ends_with(".toml") {
        let text = std::str::from_utf8(data).map_err(|_| "UnicodeError")?;
        if rel.ends_with(".json") {
            return json_loads(text).map(Some).map_err(|_| "JSONDecodeError");
        }
        return toml_doc(text).map(Some).map_err(|_| "TOMLDecodeError");
    }
    if name == "go.mod" || name == "Dockerfile" {
        return Ok(None);
    }
    if matches!(suffix(name), ".yaml" | ".yml") {
        return pyyaml::load(data).map(Some).map_err(|_| "YAMLError");
    }
    Ok(None)
}

fn ident(name: &str) -> bool {
    (1..=80).contains(&name.len()) && name.bytes().all(|b| b.is_ascii_alphanumeric() || b"_.-".contains(&b))
}

#[derive(Default)]
struct Graph {
    nodes: BTreeMap<String, Value>,
    edges: BTreeMap<(String, String, String), Value>,
    unresolved: Vec<Value>,
}

impl Graph {
    fn add_node(&mut self, uid: &str, kind: &str, label: &str, eid: &str) -> Result<(), String> {
        if self.nodes.contains_key(uid) {
            return Err("duplicate canonical node ID".into());
        }
        self.nodes.insert(uid.into(), json!({"id": uid, "kind": kind, "label": label,
                                             "evidence_id": eid, "state": "DECLARED_ONLY"}));
        Ok(())
    }
    fn add_edge(&mut self, source: &str, target: &str, relation: &str, eid: &str) -> Result<(), String> {
        if !self.nodes.contains_key(source) || !self.nodes.contains_key(target) {
            return Err("cannot invent unknown dependency node".into());
        }
        self.edges.insert((source.into(), target.into(), relation.into()),
                          json!({"source": source, "target": target, "relation": relation,
                                 "evidence_id": eid, "state": "MANIFEST_DECLARATION"}));
        Ok(())
    }
}

fn compose(graph: &mut Graph, project: &str, rel: &str, doc: &Yaml, eid: &str) -> Result<(), String> {
    let Some(Yaml::Map(services)) = doc.get("services") else {
        return Err("compose document requires a service mapping".into());
    };
    let mut names: Vec<&String> = services.iter().map(|(k, _)| k).collect();
    names.sort();
    if !names.iter().all(|n| ident(n)) {
        return Err("invalid compose service name".into());
    }
    let service = |name: &str| services.iter().find(|(k, _)| k == name).map(|(_, v)| v).unwrap();
    for name in &names {
        if !matches!(service(name), Yaml::Map(_)) {
            return Err("invalid compose service definition".into());
        }
        let uid = canonical_id("service", rel, name);
        graph.add_node(&uid, "declared_service", name, eid)?;
        graph.add_edge(project, &uid, "DECLARES", eid)?;
    }
    for name in &names {
        // A mapping lists its keys (Python's list(dict)); a sequence must hold strings.
        let keys: Vec<Option<&str>> = match service(name).get("depends_on") {
            None => Vec::new(),
            Some(Yaml::Map(entries)) => entries.iter().map(|(k, _)| Some(k.as_str())).collect(),
            Some(Yaml::Seq(items)) => items.iter()
                .map(|d| if let Yaml::Str(s) = d { Some(s.as_str()) } else { None }).collect(),
            Some(_) => return Err("invalid depends_on list".into()),
        };
        if keys.len() > 128 {
            return Err("invalid depends_on list".into());
        }
        for dep in keys {
            let Some(dep) = dep.filter(|d| ident(d)) else {
                return Err("invalid compose dependency ID".into());
            };
            let source = canonical_id("service", rel, name);
            if !names.iter().any(|n| n.as_str() == dep) {
                graph.unresolved.push(json!({"source": source, "target_name": dep,
                                             "relation": "DEPENDS_ON", "evidence_id": eid}));
            } else {
                graph.add_edge(&source, &canonical_id("service", rel, dep), "DEPENDS_ON", eid)?;
            }
        }
    }
    Ok(())
}

/// The last path component the way `pathlib.Path(arg).name` reports it.
fn pathlib_name(repo: &Path) -> String {
    match repo.components().next_back() {
        Some(Component::Normal(name)) => name.to_string_lossy().into_owned(),
        Some(Component::ParentDir) => "..".into(),
        _ => String::new(),
    }
}

/// `discover_repo.discover(repo)`.
pub fn discover(repo: &Path) -> Result<Value, String> {
    if is_symlink(repo) || !repo.is_dir() {
        return Err("expected a real local repository directory, not a symlink".into());
    }
    let walk = candidates(repo)?;
    let repo_name = pathlib_name(repo);
    let project = canonical_id("project", &repo_name, "");
    let mut graph = Graph::default();
    graph.nodes.insert(project.clone(), json!({"id": project, "kind": "project", "label": repo_name,
                                               "state": "DECLARED_ONLY"}));
    let mut evidence: BTreeMap<String, Value> = BTreeMap::new();
    let mut gaps: Vec<Value> = Vec::new();
    let mut total_bytes: u64 = 0;
    for rel in &walk.matched {
        let path = repo.join(rel);
        let regular = fs::metadata(&path).map(|m| m.is_file()).unwrap_or(false);
        if is_symlink(&path) || !regular {
            gaps.push(json!({"source": rel, "reason": "NO_LONGER_REGULAR_FILE"}));
            continue;
        }
        let size = fs::metadata(&path).map_err(|e| e.to_string())?.len();
        if size > MAX_FILE_BYTES || total_bytes + size > MAX_TOTAL_BYTES {
            gaps.push(json!({"source": rel, "reason": "SOURCE_SIZE_LIMIT"}));
            continue;
        }
        let data = fs::read(&path).map_err(|e| e.to_string())?;
        if data.len() as u64 != size || data.len() as u64 > MAX_FILE_BYTES {
            gaps.push(json!({"source": rel, "reason": "SOURCE_CHANGED_DURING_READ"}));
            continue;
        }
        total_bytes += data.len() as u64;
        let digest = hex(&Sha256::digest(&data));
        let eid = hex(&Sha256::digest(format!("{rel}:{digest}").as_bytes()));
        evidence.insert(eid.clone(), json!({"id": eid, "path": rel, "sha256": digest,
                                            "source_kind": "LOCAL_MANIFEST_BYTES", "authenticity": "UNVERIFIED"}));
        let outcome: Result<(), String> = match parse_manifest(rel, &data) {
            Err(category) => Err(category.to_string()),
            Ok(doc) => {
                let base = file_name(rel);
                let built = if COMPOSE_FILES.contains(&base) {
                    match &doc {
                        Some(d @ Yaml::Map(_)) => compose(&mut graph, &project, rel, d, &eid),
                        _ => Err("compose document requires a service mapping".into()),
                    }
                } else if rel.starts_with(".github/workflows/") {
                    let uid = canonical_id("workflow", rel, "");
                    let title = match doc.as_ref().and_then(|d| match d {
                        Yaml::Map(_) => Some(d.get("name")),
                        _ => None,
                    }) {
                        Some(Some(Yaml::Str(s))) => s.clone(),
                        _ => stem(base).to_string(),
                    };
                    graph.add_node(&uid, "workflow", &title, &eid)
                        .and_then(|_| graph.add_edge(&project, &uid, "DECLARES", &eid))
                } else {
                    let uid = canonical_id("manifest", rel, "");
                    graph.add_node(&uid, "source_manifest", rel, &eid)
                        .and_then(|_| graph.add_edge(&project, &uid, "DECLARES", &eid))
                };
                built.map_err(|_| "DiscoveryError".to_string())
            }
        };
        if let Err(category) = outcome {
            gaps.push(json!({"source": rel, "reason": "PARSE_OR_VALIDATION_FAILED", "error_type": category}));
            graph.nodes.retain(|_, n| n.get("evidence_id").and_then(Value::as_str) != Some(eid.as_str()));
            graph.edges.retain(|_, e| e["evidence_id"].as_str() != Some(eid.as_str()));
            graph.unresolved.retain(|u| u["evidence_id"].as_str() != Some(eid.as_str()));
        }
    }
    // Python's sorted() is stable; sort_by is too.
    graph.unresolved.sort_by(|a, b| {
        (a["source"].as_str(), a["target_name"].as_str()).cmp(&(b["source"].as_str(), b["target_name"].as_str()))
    });
    gaps.sort_by(|a, b| a["source"].as_str().cmp(&b["source"].as_str()));
    let files_with_evidence = evidence.len();
    let mut out = Map::new();
    out.insert("schema_version".into(), json!(1));
    out.insert("kind".into(), json!("UNREVIEWED_BUSINESS_DISCOVERY_PREVIEW"));
    out.insert("status".into(), json!("PARTIAL_SOURCE_DECLARATION_MAP"));
    out.insert("project".into(), json!(project));
    out.insert("nodes".into(), Value::Array(graph.nodes.into_values().collect()));
    out.insert("edges".into(), Value::Array(graph.edges.into_values().collect()));
    out.insert("source_evidence".into(), Value::Array(evidence.into_values().collect()));
    out.insert("unresolved".into(), Value::Array(graph.unresolved));
    out.insert("gaps".into(), Value::Array(gaps));
    out.insert("coverage".into(), json!({
        "files_seen": walk.visited, "candidate_manifests": walk.matched.len(),
        "files_with_evidence": files_with_evidence, "bytes_read": total_bytes,
        "symlinks_skipped": walk.skipped_links,
        "not_assessed": ["business_processes", "people_permissions", "financial_flows",
                         "production_runtime", "cloud_resources", "non-manifest_source_code"],
    }));
    out.insert("inferred_business_relationships".into(), json!("NONE"));
    out.insert("runtime_health".into(), json!("UNKNOWN"));
    out.insert("human_review".into(), json!("REQUIRED"));
    out.insert("execution_authorized".into(), json!(false));
    Ok(Value::Object(out))
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

/// What `python discover_repo.py --repo REPO` prints (without the trailing newline).
pub fn cli_output(repo: &Path) -> (String, i32) {
    match discover(repo) {
        Ok(value) => (pyjson::dumps(&value, None, false), 0),
        Err(error) => {
            // json.dumps({...}, sort_keys=True) with default separators and ensure_ascii.
            let mut text = String::from("{\"error\": ");
            text.push_str(&pyjson::dumps(&Value::String(error), None, true));
            text.push_str(", \"status\": \"ERROR\"}");
            (text, 1)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ids_and_names_match_python() {
        assert_eq!(canonical_id("service", "a b/compose.yml", "web"), "discovery:service:a%20b/compose.yml:web");
        assert_eq!(canonical_id("project", "é", ""), "discovery:project:%C3%A9:");
        assert_eq!(suffix(".yml"), "");
        assert_eq!(suffix("ci.YML"), ".YML");
        assert_eq!(stem(".ci.yml"), ".ci");
        assert!(eligible(".github/workflows/sub/ci.yaml"));
        assert!(!eligible("x/.github/workflows/ci.yaml"));
        assert!(eligible("x/Dockerfile"));
        assert_eq!(pathlib_name(Path::new(".")), "");
        assert_eq!(pathlib_name(Path::new("a/b/")), "b");
        assert_eq!(pathlib_name(Path::new("a/..")), "..");
    }

    #[test]
    fn json_acceptance_matches_python() {
        for ok in ["{}", " [1, -0, 1.5e3, NaN, -Infinity, \"\\ud800\"] ", "{\"name\":1,\"name\":\"x\"}"] {
            assert!(json_loads(ok).is_ok(), "{ok}");
        }
        for bad in ["", "[1,]", "{\"a\":1,}", "01", "1.", "\u{FEFF}{}", "[\"\t\"]", "{'a':1}", "-NaN", "1 2"] {
            assert!(json_loads(bad).is_err(), "{bad:?}");
        }
        assert_eq!(json_loads("{\"name\":1,\"name\":\"x\"}").unwrap().get("name"), Some(&Yaml::Str("x".into())));
    }
}

