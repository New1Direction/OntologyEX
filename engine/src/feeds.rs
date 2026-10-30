//! Live feeds: the logic of `feeds.py` in Rust, with the same configuration,
//! the same observation rows, and the same bounded JSONL store (checked in CI
//! by tests/rust_feeds_parity.py). Network I/O lives in `feeds_net`.
//!
//! Every sample is source-reported: a probe answering 200 is one answer, not
//! proof of health, and nothing here repairs or changes a system.

use crate::pyjson::{self, as_int, as_number, py_round};
use chrono::{DateTime, Datelike, Duration as ChronoDuration, NaiveDate, NaiveDateTime, NaiveTime, Utc};
use serde_json::{json, Map, Value};
use std::collections::{BTreeMap, BTreeSet, HashMap, VecDeque};
use std::fs;
use std::io::{Read, Write};
use std::net::Ipv6Addr;
use std::path::{Path, PathBuf};

pub const FEED_KIND: &str = "ONTOLOGYEX_FEEDS";
pub const MAX_CONFIG_BYTES: usize = 65536;
pub const MAX_TARGETS: usize = 256;
pub const MAX_BODY: usize = 1024 * 1024;
pub const MAX_OTLP_BODY: usize = 4 * 1024 * 1024;
pub const MAX_OTLP_DECOMPRESSED: usize = 16 * 1024 * 1024;
pub const MAX_SPANS_PER_SERVICE: usize = 100_000;
pub const KEEP_PER_COMPONENT: usize = 8;
pub const MAX_JSON_DEPTH: usize = 64;
pub const MAX_OBSERVATION_BYTES: usize = 1024 * 1024;
pub const MAX_OBSERVATIONS: usize = 4096;
pub const METRIC_RULES: [&str; 4] = ["error_rate_pct", "p95_latency_ms", "probe_latency_ms", "job_lag_seconds"];
const METRICS: [&str; 6] = ["queue_lag_seconds", "db_pool_utilization_pct", "error_rate_pct",
                            "p95_latency_ms", "job_lag_seconds", "probe_latency_ms"];
const PERCENT_METRICS: [&str; 2] = ["db_pool_utilization_pct", "error_rate_pct"];
const STATUSES: [&str; 3] = ["ok", "warning", "error"];
const SOURCES: [&str; 5] = ["otel", "health_probe", "prometheus", "manual_export", "synthetic"];

pub type Res<T> = Result<T, String>;

fn check(ok: bool, message: impl Into<String>) -> Res<()> {
    if ok { Ok(()) } else { Err(message.into()) }
}

// ---------------------------------------------------------------- strict JSON

/// `feeds.json_document`: UTF-8, no NaN/Infinity, finite numbers, valid Unicode,
/// at most `MAX_JSON_DEPTH` levels. `strict_keys` refuses repeated object keys.
pub fn json_document(body: &[u8], where_: &str, strict_keys: bool) -> Res<Value> {
    let parsed = if strict_keys {
        pyjson::parse_strict(body)
    } else {
        serde_json::from_slice::<Value>(body).map_err(|e| e.to_string())
    };
    let value = parsed.map_err(|_| format!("invalid {where_} JSON"))?;
    let mut stack = vec![(&value, 1usize)];
    while let Some((v, depth)) = stack.pop() {
        match v {
            Value::Array(items) => {
                check(depth <= MAX_JSON_DEPTH, format!("{where_} JSON nested too deeply"))?;
                stack.extend(items.iter().map(|i| (i, depth + 1)));
            }
            Value::Object(map) => {
                check(depth <= MAX_JSON_DEPTH, format!("{where_} JSON nested too deeply"))?;
                stack.extend(map.values().map(|i| (i, depth + 1)));
            }
            _ => {}
        }
    }
    Ok(value)
}

// ---------------------------------------------------------------- configuration

#[derive(Debug, Clone, Default)]
pub struct Thresholds(pub BTreeMap<String, f64>);

#[derive(Debug, Clone)]
pub struct Probe {
    pub component: String,
    pub url: String,
    pub interval: i64,
    pub timeout: i64,
    pub expect: Vec<i64>,
    pub warning: Thresholds,
}

#[derive(Debug, Clone)]
pub struct PromTarget {
    pub component: String,
    pub up: Option<String>,
    pub error_rate_pct: Option<String>,
    pub p95_latency_ms: Option<String>,
    /// How long ago a job last did its work (an indexer's last block, a writer's last write).
    pub job_lag_seconds: Option<String>,
    pub warning: Thresholds,
    pub error: Thresholds,
}

#[derive(Debug, Clone)]
pub struct Prometheus {
    pub url: String,
    pub token_env: String,
    pub interval: i64,
    pub timeout: i64,
    pub targets: Vec<PromTarget>,
}

#[derive(Debug, Clone)]
pub struct Otlp {
    pub services: BTreeMap<String, String>,
    pub bind: String,
    pub port: u16,
    pub token_env: String,
    pub window: i64,
    pub emit: i64,
    pub min_requests: usize,
    pub warning: Thresholds,
    pub error: Thresholds,
}

#[derive(Debug, Clone)]
pub struct Config {
    pub output: PathBuf,
    pub keep: usize,
    pub probes: Vec<Probe>,
    pub prometheus: Option<Prometheus>,
    pub otlp: Option<Otlp>,
}

fn keys<'a>(value: &'a Value, required: &[&str], optional: &[&str], where_: &str) -> Res<&'a Map<String, Value>> {
    let map = value.as_object().ok_or(format!("{where_}: expected an object"))?;
    let ok = required.iter().all(|k| map.contains_key(*k))
        && map.keys().all(|k| required.contains(&k.as_str()) || optional.contains(&k.as_str()));
    check(ok, format!("{where_}: unexpected or missing fields"))?;
    Ok(map)
}

fn seconds(value: Option<&Value>, default: i64, low: i64, high: i64, where_: &str) -> Res<i64> {
    let Some(value) = value else { return Ok(default) };
    match as_int(value) {
        Some(v) if (low..=high).contains(&v) => Ok(v),
        _ => Err(format!("{where_}: expected {low}..{high} seconds")),
    }
}

pub fn thresholds(value: Option<&Value>, where_: &str) -> Res<Thresholds> {
    let value = match value {
        None | Some(Value::Null) => return Ok(Thresholds::default()),
        Some(v) => v,
    };
    let map = keys(value, &[], &METRIC_RULES, where_)?;
    let mut out = BTreeMap::new();
    for (name, limit) in map {
        let limit = as_number(limit).filter(|l| l.is_finite() && (0.0..=1e9).contains(l))
            .ok_or(format!("{where_}.{name}: invalid threshold"))?;
        check(name != "error_rate_pct" || limit <= 100.0, format!("{where_}.{name}: percentage above 100"))?;
        out.insert(name.clone(), limit);
    }
    Ok(Thresholds(out))
}

/// `^discovery:[a-z_]+:[A-Za-z0-9_.~/%:-]{1,600}$`
pub fn component_ok(id: &str) -> bool {
    let Some(rest) = id.strip_prefix("discovery:") else { return false };
    let tail_ok = |t: &str| {
        (1..=600).contains(&t.len())
            && t.bytes().all(|b| b.is_ascii_alphanumeric() || b"_.~/%:-".contains(&b))
    };
    let kind_len = rest.bytes().take_while(|b| b.is_ascii_lowercase() || *b == b'_').count();
    // [a-z_]+ is greedy but the tail may also hold [a-z_:]: try every split.
    (1..=kind_len).any(|i| rest.as_bytes().get(i) == Some(&b':') && tail_ok(&rest[i + 1..]))
}

/// `feeds.SERVICE.fullmatch`: a plain service.name.
pub fn service_ok(name: &str) -> bool {
    (1..=200).contains(&name.len()) && name.bytes().all(|b| b.is_ascii_alphanumeric() || b"_.:/@-".contains(&b))
}

fn component(value: Option<&Value>, where_: &str) -> Res<String> {
    match value.and_then(Value::as_str) {
        Some(s) if component_ok(s) => Ok(s.to_string()),
        _ => Err(format!("{where_}: expected a discovered component ID (discovery:...)")),
    }
}

/// Python's `ipaddress.IPv4Address`: four decimal octets, no leading zeros.
pub fn strict_ipv4(host: &str) -> bool {
    let parts: Vec<&str> = host.split('.').collect();
    parts.len() == 4 && parts.iter().all(|p| {
        !p.is_empty() && p.len() <= 3 && p.bytes().all(|b| b.is_ascii_digit())
            && !(p.len() > 1 && p.starts_with('0')) && p.parse::<u16>().is_ok_and(|v| v <= 255)
    })
}

fn host_ok(host: &str, bracketed: bool) -> bool {
    if bracketed {
        return !host.contains('%') && host.parse::<Ipv6Addr>().is_ok();
    }
    let label_ok = |l: &str| !l.is_empty() && l.bytes().all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_' || b == b'-');
    if !host.split('.').all(label_ok) {
        return false;
    }
    let last = host.rsplit('.').next().unwrap_or(host);
    if last.bytes().all(|b| b.is_ascii_digit()) || last.starts_with("0x") {
        return strict_ipv4(host);
    }
    true
}

/// Scheme, netloc, path, query of `urllib.parse.urlsplit` for an RFC 3986 string without `#`.
fn urlsplit(url: &str) -> (String, &str, &str, &str) {
    let mut scheme = String::new();
    let mut rest = url;
    if let Some(i) = url.find(':') {
        let head = &url[..i];
        if i > 0 && head.as_bytes()[0].is_ascii_alphabetic()
            && head.bytes().all(|b| b.is_ascii_alphanumeric() || b"+-.".contains(&b)) {
            scheme = head.to_ascii_lowercase();
            rest = &url[i + 1..];
        }
    }
    let mut netloc = "";
    if let Some(after) = rest.strip_prefix("//") {
        let end = after.find(['/', '?', '#']).unwrap_or(after.len());
        netloc = &after[..end];
        rest = &after[end..];
    }
    let (path, query) = match rest.split_once('?') {
        Some((p, q)) => (p, q),
        None => (rest, ""),
    };
    (scheme, netloc, path, query)
}

/// `urllib.parse.urlsplit(target).path` for an HTTP request target.
pub fn request_path(target: &str) -> &str {
    let mut rest = target;
    if let Some(i) = target.find(':') {
        let head = &target[..i];
        if i > 0 && head.as_bytes()[0].is_ascii_alphabetic()
            && head.bytes().all(|b| b.is_ascii_alphanumeric() || b"+-.".contains(&b)) {
            rest = &target[i + 1..];
        }
    }
    if let Some(after) = rest.strip_prefix("//") {
        rest = &after[after.find(['/', '?', '#']).unwrap_or(after.len())..];
    }
    &rest[..rest.find(['?', '#']).unwrap_or(rest.len())]
}

/// `feeds._url`: an allowlisted http(s) URL with no credentials or fragment.
pub fn url(value: Option<&Value>, hosts: &BTreeSet<String>, where_: &str) -> Res<String> {
    let text = value.and_then(Value::as_str).filter(|s| {
        (1..=2048).contains(&s.len())
            && s.bytes().all(|b| b.is_ascii_alphanumeric() || b"-._~:/?[]@!$&'()*+,;=%".contains(&b))
    }).ok_or(format!("{where_}: invalid URL (printable RFC 3986 characters, at most 2048)"))?;
    check(!text.contains('#'), format!("{where_}: no URL fragment"))?;
    let (scheme, netloc, path, query) = urlsplit(text);
    let invalid = || format!("{where_}: invalid URL");
    // Brackets must pair, and only around a host at the start of the host part.
    let hostinfo = netloc.rsplit_once('@').map_or(netloc, |(_, h)| h);
    let has_open = netloc.contains('[');
    if has_open != netloc.contains(']') {
        return Err(invalid());
    }
    let hostname = if has_open {
        // urlsplit: nothing before '[', and only a port after ']'.
        let (before, bracketed) = hostinfo.split_once('[').ok_or_else(invalid)?;
        check(before.is_empty(), invalid())?;
        let (host, after) = bracketed.split_once(']').ok_or_else(invalid)?;
        check(after.is_empty() || after.starts_with(':'), invalid())?;
        host.to_string()
    } else {
        hostinfo.split_once(':').map_or(hostinfo, |(h, _)| h).to_string()
    };
    // The port is all digits within 0..65535.
    let port = hostinfo.rsplit_once(']').map_or(hostinfo, |(_, p)| p);
    let port = port.split_once(':').map_or("", |(_, p)| p);
    check(port.bytes().all(|b| b.is_ascii_digit()), format!("{where_}: invalid port"))?;
    check(port.is_empty() || port.trim_start_matches('0').len() <= 5 && port.parse::<u64>().is_ok_and(|p| p <= 65535)
          || port.bytes().all(|b| b == b'0'), invalid())?;
    let prefix_ok = text.len() >= scheme.len() + 3 && text[..scheme.len() + 3].to_ascii_lowercase() == format!("{scheme}://");
    check((scheme == "http" || scheme == "https") && prefix_ok, format!("{where_}: only http and https"))?;
    check(!netloc.contains('@'), format!("{where_}: never put credentials in a URL; use token_env"))?;
    check(!path.contains(['[', ']']) && !query.contains(['[', ']']), format!("{where_}: brackets only around an IPv6 host"))?;
    let hostname = hostname.to_lowercase();
    check(!hostname.is_empty() && host_ok(&hostname, has_open), format!("{where_}: invalid host"))?;
    check(hosts.contains(&hostname), format!("{where_}: host {hostname:?} is not in allowed_hosts"))?;
    Ok(text.to_string())
}

fn ip_loopback(bind: &str) -> Res<bool> {
    check(!bind.contains('%'), "otlp.bind must be an IP address")?;
    if strict_ipv4(bind) {
        return Ok(bind.starts_with("127."));
    }
    if bind.contains('.') && !bind.contains(':') {
        return Err("otlp.bind must be an IP address".into());
    }
    let v6: Ipv6Addr = bind.parse().map_err(|_| "otlp.bind must be an IP address".to_string())?;
    Ok(v6 == Ipv6Addr::LOCALHOST)
}

fn python_len(s: &str) -> usize {
    s.chars().count()
}

/// `feeds.load_config`.
pub fn load_config(path: &Path) -> Res<Config> {
    let meta = fs::symlink_metadata(path).map_err(|_| "config must be a regular file".to_string())?;
    check(meta.file_type().is_file(), "config must be a regular file")?;
    let data = fs::read(path).map_err(|e| e.to_string())?;
    check(data.len() <= MAX_CONFIG_BYTES, "config too large")?;
    let config = json_document(&data, "config", true)?;
    let map = keys(&config, &["schema_version", "kind", "output"],
                   &["allowed_hosts", "keep_per_component", "probes", "prometheus", "otlp"], "config")?;
    // Python compares with ==, so 1, 1.0, and true all equal 1.
    let version_one = match &map["schema_version"] {
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64() == Some(1.0),
        _ => false,
    };
    check(version_one && map["kind"].as_str() == Some(FEED_KIND), "unsupported feed config")?;
    let output = map["output"].as_str().filter(|o| o.ends_with(".jsonl")).ok_or("output must be a .jsonl path")?;
    let hosts_value = map.get("allowed_hosts").cloned().unwrap_or(json!([]));
    let hosts_list = hosts_value.as_array().filter(|h| h.len() <= 64 && h.iter().all(|x| {
        x.as_str().is_some_and(|s| (1..=253).contains(&python_len(s)))
    })).ok_or("invalid allowed_hosts")?;
    let hosts: BTreeSet<String> = hosts_list.iter().filter_map(Value::as_str).map(str::to_lowercase).collect();
    let keep = match map.get("keep_per_component") {
        None => KEEP_PER_COMPONENT as i64,
        Some(v) => as_int(v).filter(|k| (2..=64).contains(k)).ok_or("keep_per_component must be 2..64")?,
    };
    check(["probes", "prometheus", "otlp"].iter().any(|k| map.contains_key(*k)), "configure at least one feed")?;

    let probes_value = map.get("probes").cloned().unwrap_or(json!([]));
    let probe_list = probes_value.as_array().filter(|p| p.len() <= MAX_TARGETS).ok_or("invalid probes")?;
    let mut probes = Vec::new();
    for (i, probe) in probe_list.iter().enumerate() {
        let where_ = format!("probes[{i}]");
        let p = keys(probe, &["component", "url"], &["interval_seconds", "timeout_seconds", "expect_status", "warning"], &where_)?;
        let component = component(p.get("component"), &where_)?;
        let url = url(p.get("url"), &hosts, &where_)?;
        let interval = seconds(p.get("interval_seconds"), 30, 5, 3600, &where_)?;
        let timeout = seconds(p.get("timeout_seconds"), 3, 1, 30, &where_)?;
        let expect = match p.get("expect_status") {
            None => vec![200],
            Some(v) => {
                let list = v.as_array().filter(|l| (1..=16).contains(&l.len())).ok_or(format!("{where_}: invalid expect_status"))?;
                list.iter().map(|c| as_int(c).filter(|c| (100..=599).contains(c)).ok_or(format!("{where_}: invalid expect_status")))
                    .collect::<Res<Vec<i64>>>()?
            }
        };
        let warning = thresholds(p.get("warning"), &format!("{where_}.warning"))?;
        probes.push(Probe { component, url, interval, timeout, expect, warning });
    }

    let mut prometheus = None;
    if let Some(prom) = map.get("prometheus").filter(|v| !v.is_null()) {
        let p = keys(prom, &["url", "targets"], &["token_env", "interval_seconds", "timeout_seconds"], "prometheus")?;
        let url = url(p.get("url"), &hosts, "prometheus.url")?;
        let token_env = match p.get("token_env") {
            None => String::new(),
            Some(v) => v.as_str().filter(|s| python_len(s) <= 64).ok_or("prometheus.token_env must name an environment variable")?.to_string(),
        };
        let interval = seconds(p.get("interval_seconds"), 30, 5, 3600, "prometheus")?;
        let timeout = seconds(p.get("timeout_seconds"), 5, 1, 30, "prometheus")?;
        let target_list = p["targets"].as_array().filter(|t| (1..=MAX_TARGETS).contains(&t.len())).ok_or("invalid prometheus.targets")?;
        let mut targets = Vec::new();
        for (i, target) in target_list.iter().enumerate() {
            let where_ = format!("prometheus.targets[{i}]");
            let t = keys(target, &["component"], &["up", "error_rate_pct", "p95_latency_ms", "job_lag_seconds", "warning", "error"], &where_)?;
            let component = component(t.get("component"), &where_)?;
            let query = |k: &str| t.get(k);
            let present: Vec<&Value> = ["up", "error_rate_pct", "p95_latency_ms", "job_lag_seconds"].iter().filter_map(|k| query(k)).collect();
            check(!present.is_empty() && present.iter().all(|q| q.as_str().is_some_and(|s| (1..=2000).contains(&python_len(s)))),
                  format!("{where_}: give at least one PromQL query"))?;
            let text = |k: &str| query(k).and_then(Value::as_str).map(str::to_string);
            let warning = thresholds(t.get("warning"), &format!("{where_}.warning"))?;
            let error = thresholds(t.get("error"), &format!("{where_}.error"))?;
            targets.push(PromTarget { component, up: text("up"), error_rate_pct: text("error_rate_pct"),
                                      p95_latency_ms: text("p95_latency_ms"), job_lag_seconds: text("job_lag_seconds"),
                                      warning, error });
        }
        prometheus = Some(Prometheus { url, token_env, interval, timeout, targets });
    }

    let mut otlp = None;
    if let Some(o) = map.get("otlp").filter(|v| !v.is_null()) {
        let o = keys(o, &["services"], &["bind", "port", "token_env", "window_seconds", "emit_seconds",
                                         "min_requests", "warning", "error"], "otlp")?;
        let bind = match o.get("bind") {
            None => "127.0.0.1".to_string(),
            Some(v) => v.as_str().ok_or("otlp.bind must be an IP address")?.to_string(),
        };
        let loopback = ip_loopback(&bind)?;
        let token_env = match o.get("token_env") {
            None => String::new(),
            Some(v) => v.as_str().filter(|s| python_len(s) <= 64).ok_or("otlp.token_env must name a variable")?.to_string(),
        };
        check(loopback || !token_env.is_empty(), "a non-loopback OTLP listener requires token_env")?;
        let port = match o.get("port") {
            None => 4318,
            Some(v) => as_int(v).filter(|p| (0..=65535).contains(p)).ok_or("invalid otlp.port")?,
        } as u16;
        let services_map = o["services"].as_object().filter(|s| (1..=MAX_TARGETS).contains(&s.len())).ok_or("invalid otlp.services")?;
        let mut services = BTreeMap::new();
        for (name, comp) in services_map {
            check(service_ok(name), format!("otlp.services: invalid service.name {name:?}"))?;
            services.insert(name.clone(), component(Some(comp), &format!("otlp.services[{name}]"))?);
        }
        let window = seconds(o.get("window_seconds"), 60, 10, 3600, "otlp.window_seconds")?;
        let emit = seconds(o.get("emit_seconds"), 15, 1, 600, "otlp.emit_seconds")?;
        let min_requests = match o.get("min_requests") {
            None => 5,
            Some(v) => as_int(v).filter(|m| (1..=100000).contains(m)).ok_or("otlp.min_requests must be 1..100000")?,
        } as usize;
        let warning = thresholds(o.get("warning"), "otlp.warning")?;
        let error = thresholds(o.get("error"), "otlp.error")?;
        otlp = Some(Otlp { services, bind, port, token_env, window, emit, min_requests, warning, error });
    }
    let root = fs::canonicalize(path).map_err(|e| e.to_string())?.parent().map(Path::to_path_buf).unwrap_or_default();
    let out = Path::new(output);
    let output = if out.is_absolute() { out.to_path_buf() } else { root.join(out) };
    Ok(Config { output, keep: keep as usize, probes, prometheus, otlp })
}

impl Config {
    /// The effective configuration with every default filled in (for `validate`).
    pub fn describe(&self) -> Value {
        let th = |t: &Thresholds| json!(t.0);
        json!({
            "output": self.output.to_string_lossy(),
            "keep_per_component": self.keep,
            "probes": self.probes.iter().map(|p| json!({
                "component": p.component, "url": p.url, "interval_seconds": p.interval,
                "timeout_seconds": p.timeout, "expect_status": p.expect, "warning": th(&p.warning)})).collect::<Vec<_>>(),
            "prometheus": self.prometheus.as_ref().map(|p| json!({
                "url": p.url, "token_env": p.token_env, "interval_seconds": p.interval, "timeout_seconds": p.timeout,
                "targets": p.targets.iter().map(|t| json!({
                    "component": t.component, "up": t.up, "error_rate_pct": t.error_rate_pct,
                    "p95_latency_ms": t.p95_latency_ms, "job_lag_seconds": t.job_lag_seconds, "warning": th(&t.warning),
                    "error": th(&t.error)})).collect::<Vec<_>>()})),
            "otlp": self.otlp.as_ref().map(|o| json!({
                "services": o.services, "bind": o.bind, "port": o.port, "token_env": o.token_env,
                "window_seconds": o.window, "emit_seconds": o.emit, "min_requests": o.min_requests,
                "warning": th(&o.warning), "error": th(&o.error)})),
        })
    }

    pub fn status_path(&self) -> PathBuf {
        self.output.with_extension("status.json")
    }

    pub fn calls_path(&self) -> PathBuf {
        self.output.with_extension("calls.json")
    }
}

// ---------------------------------------------------------------- rows

pub fn ttl(interval: i64) -> i64 {
    (3 * interval).clamp(60, 86400)
}

/// At or above an error threshold is error; at or above a warning threshold is warning.
pub fn classify(metrics: &BTreeMap<String, f64>, warning: &Thresholds, error: &Thresholds, down: bool) -> &'static str {
    let hit = |t: &Thresholds| t.0.iter().any(|(k, v)| metrics.get(k).is_some_and(|m| m >= v));
    if down || hit(error) {
        "error"
    } else if hit(warning) {
        "warning"
    } else {
        "ok"
    }
}

/// One stored observation; metrics keep Python's int/float distinction.
#[derive(Debug, Clone, PartialEq)]
pub struct Row {
    pub component_id: String,
    pub observed_at: String,
    pub status: String,
    pub source: String,
    pub expires_after_seconds: i64,
    pub metrics: BTreeMap<String, serde_json::Number>,
}

impl Row {
    /// `json.dumps(row, sort_keys=True, separators=(",", ":"))`
    pub fn line(&self) -> String {
        let metrics: Map<String, Value> = self.metrics.iter().map(|(k, v)| (k.clone(), Value::Number(v.clone()))).collect();
        pyjson::dumps(&json!({
            "component_id": self.component_id, "expires_after_seconds": self.expires_after_seconds,
            "metrics": metrics, "observed_at": self.observed_at, "schema_version": 1,
            "source": self.source, "status": self.status,
        }), None, true)
    }
}

pub fn stamp(moment: DateTime<Utc>) -> String {
    moment.format("%Y-%m-%dT%H:%M:%SZ").to_string()
}

/// `feeds.observation`.
pub fn observation(component: &str, status: &str, source: &str, interval: i64,
                   metrics: &BTreeMap<String, f64>, moment: DateTime<Utc>) -> Row {
    let mut clean = BTreeMap::new();
    for (k, v) in metrics {
        if !v.is_finite() {
            continue;
        }
        let mut x = py_round(*v, 3);
        if k == "error_rate_pct" {
            x = min_(100.0, max_(0.0, x));
        }
        x = min_(1e9, max_(0.0, x));
        clean.insert(k.clone(), serde_json::Number::from_f64(x).expect("finite"));
    }
    Row { component_id: component.into(), observed_at: stamp(moment), status: status.into(), source: source.into(),
          expires_after_seconds: ttl(interval), metrics: clean }
}

/// Python's max(a, b): a unless b > a.
fn max_(a: f64, b: f64) -> f64 { if b > a { b } else { a } }
/// Python's min(a, b): a unless b < a.
fn min_(a: f64, b: f64) -> f64 { if b < a { b } else { a } }

// ---------------------------------------------------------------- store

/// `control_room.timestamp` for RFC 3339 forms: the instant in UTC, written like
/// Python's `isoformat()` with `Z`.
pub fn normalize_timestamp(value: &str) -> Res<String> {
    let bad = || "invalid observation timestamp".to_string();
    let zoned = value.ends_with('Z') || {
        let b = value.as_bytes();
        b.len() >= 6 && matches!(b[b.len() - 6], b'+' | b'-') && b[b.len() - 5..b.len() - 3].iter().all(u8::is_ascii_digit)
            && b[b.len() - 3] == b':' && b[b.len() - 2..].iter().all(u8::is_ascii_digit)
    };
    check(value.len() <= 45 && value.contains('T') && zoned, "timestamp needs an explicit RFC3339 timezone")?;
    let (date, rest) = value.split_once('T').ok_or_else(bad)?;
    let (clock, offset_minutes) = if let Some(c) = rest.strip_suffix('Z') {
        (c, 0i64)
    } else {
        let (c, z) = rest.split_at(rest.len() - 6);
        let sign = if z.starts_with('-') { -1 } else { 1 };
        let (h, m): (i64, i64) = (z[1..3].parse().map_err(|_| bad())?, z[4..6].parse().map_err(|_| bad())?);
        check(h <= 23 && m <= 59, bad())?;
        (c, sign * (h * 60 + m))
    };
    let digits = |s: &str, n: usize| s.len() == n && s.bytes().all(|b| b.is_ascii_digit());
    let d: Vec<&str> = date.split('-').collect();
    check(d.len() == 3 && digits(d[0], 4) && digits(d[1], 2) && digits(d[2], 2), bad())?;
    let (hms, fraction) = match clock.split_once('.') {
        Some((hms, fraction)) => {
            check(!fraction.is_empty() && fraction.bytes().all(|b| b.is_ascii_digit()), bad())?;
            (hms, fraction)
        }
        None => (clock, ""),
    };
    let t: Vec<&str> = hms.split(':').collect();
    check((t.len() == 2 || t.len() == 3) && t.iter().all(|p| digits(p, 2)) && (t.len() == 3 || fraction.is_empty()), bad())?;
    let year: i32 = d[0].parse().map_err(|_| bad())?;
    check(year >= 1, bad())?;
    let date = NaiveDate::from_ymd_opt(year, d[1].parse().map_err(|_| bad())?, d[2].parse().map_err(|_| bad())?).ok_or_else(bad)?;
    let micros: u32 = if fraction.is_empty() { 0 } else { format!("{:0<6}", &fraction[..fraction.len().min(6)]).parse().map_err(|_| bad())? };
    let second = if t.len() == 3 { t[2].parse().map_err(|_| bad())? } else { 0 };
    let time = NaiveTime::from_hms_micro_opt(t[0].parse().map_err(|_| bad())?, t[1].parse().map_err(|_| bad())?, second, micros)
        .ok_or_else(bad)?;
    let utc = NaiveDateTime::new(date, time).checked_sub_signed(ChronoDuration::minutes(offset_minutes)).ok_or_else(bad)?;
    check((1..=9999).contains(&utc.date().year_ce().1) && utc.date().year_ce().0, bad())?;
    let mut out = format!("{:04}-{}", utc.date().year_ce().1, utc.format("%m-%dT%H:%M:%S"));
    let micro = utc.and_utc().timestamp_subsec_micros();
    if micro != 0 {
        out.push_str(&format!(".{micro:06}"));
    }
    out.push('Z');
    Ok(out)
}


pub fn row_from(value: &Value) -> Res<Row> {
    let map = value.as_object().ok_or("invalid observation shape")?;
    let want = ["schema_version", "component_id", "observed_at", "status", "source", "expires_after_seconds", "metrics"];
    check(map.len() == want.len() && want.iter().all(|k| map.contains_key(*k)), "invalid observation shape")?;
    check(as_int(&map["schema_version"]) == Some(1), "unknown observation schema")?;
    let component_id = map["component_id"].as_str().filter(|c| component_ok(c)).ok_or("invalid component ID")?;
    let status = map["status"].as_str().filter(|s| STATUSES.contains(s));
    let source = map["source"].as_str().filter(|s| SOURCES.contains(s));
    let (Some(status), Some(source)) = (status, source) else { return Err("invalid status/source".into()) };
    let ttl = as_int(&map["expires_after_seconds"]).filter(|t| (1..=86400).contains(t)).ok_or("invalid freshness TTL")?;
    let metrics_map = map["metrics"].as_object().filter(|m| m.len() <= METRICS.len() && m.keys().all(|k| METRICS.contains(&k.as_str())))
        .ok_or("invalid metric names")?;
    let mut metrics = BTreeMap::new();
    for (name, v) in metrics_map {
        let Value::Number(n) = v else { return Err("invalid metric value".into()) };
        let x = n.as_f64().unwrap_or(f64::NAN);
        check(x.is_finite() && (0.0..=1e9).contains(&x), "invalid metric value")?;
        check(!PERCENT_METRICS.contains(&name.as_str()) || x <= 100.0, "percentage metric exceeds 100")?;
        metrics.insert(name.clone(), n.clone());
    }
    let observed_at = normalize_timestamp(map["observed_at"].as_str().ok_or("timestamp needs an explicit RFC3339 timezone")?)?;
    Ok(Row { component_id: component_id.into(), observed_at, status: status.into(), source: source.into(),
             expires_after_seconds: ttl, metrics })
}

#[cfg(target_os = "linux")]
const O_NOFOLLOW_NONBLOCK: i32 = 0o400000 | 0o4000;
#[cfg(target_os = "macos")]
const O_NOFOLLOW_NONBLOCK: i32 = 0x0100 | 0x0004;
#[cfg(not(any(target_os = "linux", target_os = "macos")))]
const O_NOFOLLOW_NONBLOCK: i32 = 0;

/// `control_room.safe_read`: a bounded regular file, never through a final symlink.
pub fn safe_read(path: &Path, max: usize) -> Res<Vec<u8>> {
    use std::os::unix::fs::OpenOptionsExt;
    let meta = fs::symlink_metadata(path).map_err(|e| e.to_string())?;
    check(!meta.file_type().is_symlink(), "symlinked input refused")?;
    let mut file = fs::OpenOptions::new().read(true).custom_flags(O_NOFOLLOW_NONBLOCK).open(path).map_err(|e| e.to_string())?;
    let meta = file.metadata().map_err(|e| e.to_string())?;
    check(meta.is_file(), "expected a regular file")?;
    check(meta.len() as usize <= max, "input exceeds file budget")?;
    let mut data = Vec::new();
    (&mut file).take(max as u64 + 1).read_to_end(&mut data).map_err(|e| e.to_string())?;
    check(data.len() <= max, "input exceeds file budget")?;
    Ok(data)
}

/// `control_room.read_observations` (RFC 3339 timestamps).
pub fn read_observations(path: &Path) -> Res<Vec<Row>> {
    let raw = safe_read(path, MAX_OBSERVATION_BYTES)?;
    let mut rows = Vec::new();
    // bytes.splitlines(): \n, \r\n, and \r end a line.
    let mut lines = Vec::new();
    let mut start = 0;
    let mut i = 0;
    while i < raw.len() {
        if raw[i] == b'\n' || raw[i] == b'\r' {
            lines.push(&raw[start..i]);
            if raw[i] == b'\r' && raw.get(i + 1) == Some(&b'\n') {
                i += 1;
            }
            start = i + 1;
        }
        i += 1;
    }
    if start < raw.len() {
        lines.push(&raw[start..]);
    }
    for line in lines {
        if line.iter().all(|b| b" \t\n\r\x0b\x0c".contains(b)) {
            continue;
        }
        check(rows.len() < MAX_OBSERVATIONS && line.len() <= 8192, "observation count/line limit")?;
        let value = pyjson::parse_strict(line).map_err(|e| format!("invalid observation JSON: {e}"))?;
        rows.push(row_from(&value)?);
    }
    Ok(rows)
}

fn temp_name(dir: &Path, prefix: &str) -> PathBuf {
    use std::sync::atomic::{AtomicU64, Ordering};
    static COUNTER: AtomicU64 = AtomicU64::new(0);
    let n = COUNTER.fetch_add(1, Ordering::Relaxed);
    let nanos = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map_or(0, |d| d.subsec_nanos());
    dir.join(format!("{prefix}{}-{n}-{nanos}", std::process::id()))
}

/// Write `text` to `path` atomically (temporary file, fsync, rename).
pub fn atomic_write(path: &Path, text: &[u8], prefix: &str, sync: bool) -> Res<()> {
    let dir = path.parent().unwrap_or(Path::new("."));
    fs::create_dir_all(dir).map_err(|e| e.to_string())?;
    let temp = temp_name(dir, prefix);
    let result = (|| {
        let mut file = fs::OpenOptions::new().write(true).create_new(true).open(&temp)?;
        file.write_all(text)?;
        if sync {
            file.sync_all()?;
        }
        fs::rename(&temp, path)
    })();
    if result.is_err() {
        let _ = fs::remove_file(&temp);
    }
    result.map_err(|e| e.to_string())
}

/// `feeds.retain`: the newest `keep` samples per component (stable sort, components in order).
pub fn retain(rows: Vec<Row>, keep: usize) -> Vec<Row> {
    let mut by_component: BTreeMap<String, Vec<Row>> = BTreeMap::new();
    for row in rows {
        by_component.entry(row.component_id.clone()).or_default().push(row);
    }
    let mut kept = Vec::new();
    for (_, mut samples) in by_component {
        samples.sort_by(|a, b| (&a.observed_at, &a.source, &a.status).cmp(&(&b.observed_at, &b.source, &b.status)));
        let from = samples.len().saturating_sub(keep);
        kept.extend(samples.drain(from..));
    }
    kept
}

/// `feeds.ObservationStore.add`: keep the newest samples per component, rewrite atomically.
pub fn store_add(path: &Path, keep: usize, rows: Vec<Row>) -> Res<usize> {
    if rows.is_empty() {
        return Ok(0);
    }
    let added = rows.len();
    // Path.exists() follows links: a dangling link is replaced, a live one refused by the reader.
    let mut existing = if path.exists() { read_observations(path)? } else { Vec::new() };
    existing.extend(rows);
    let kept = retain(existing, keep);
    let text: String = kept.iter().map(|r| r.line() + "\n").collect();
    check(kept.len() <= MAX_OBSERVATIONS && text.len() <= MAX_OBSERVATION_BYTES,
          "observation store budget exceeded; lower keep_per_component")?;
    atomic_write(path, text.as_bytes(), ".observations-", true)?;
    read_observations(path)?; // the control room must accept it
    Ok(added)
}

// ---------------------------------------------------------------- Prometheus

/// `urllib.parse.urlencode({"query": q})`.
pub fn prometheus_query_url(base: &str, query: &str) -> String {
    let mut out = format!("{}/api/v1/query?query=", base.trim_end_matches('/'));
    for &b in query.as_bytes() {
        match b {
            b' ' => out.push('+'),
            b if b.is_ascii_alphanumeric() || b"_.-~".contains(&b) => out.push(b as char),
            b => out.push_str(&format!("%{b:02X}")),
        }
    }
    out
}

fn prom_number_ok(s: &str) -> bool {
    if matches!(s, "NaN" | "Inf" | "+Inf" | "-Inf") {
        return true;
    }
    let body = s.strip_prefix(['+', '-']).unwrap_or(s);
    let (mantissa, exponent) = match body.find(['e', 'E']) {
        Some(i) => (&body[..i], Some(&body[i + 1..])),
        None => (body, None),
    };
    let mantissa_ok = match mantissa.split_once('.') {
        Some((a, b)) => (!a.is_empty() || !b.is_empty()) && a.bytes().all(|c| c.is_ascii_digit()) && b.bytes().all(|c| c.is_ascii_digit())
            && !(a.is_empty() && b.is_empty()),
        None => !mantissa.is_empty() && mantissa.bytes().all(|c| c.is_ascii_digit()),
    };
    let exponent_ok = exponent.is_none_or(|e| {
        let e = e.strip_prefix(['+', '-']).unwrap_or(e);
        !e.is_empty() && e.bytes().all(|c| c.is_ascii_digit())
    });
    mantissa_ok && exponent_ok
}

/// `feeds.prometheus_number`: one finite scalar from an instant query with one series.
pub fn prometheus_number(body: &[u8]) -> Res<f64> {
    let data = json_document(body, "prometheus", false)?;
    let unexpected = |kind: &str| format!("unexpected prometheus response: {kind}");
    check(data.as_object().is_some_and(|d| d.get("status").and_then(Value::as_str) == Some("success")), "prometheus query failed")?;
    let inner = data.get("data").ok_or_else(|| unexpected("KeyError"))?;
    let inner = inner.as_object().ok_or_else(|| unexpected("TypeError"))?;
    let result = inner.get("result").ok_or_else(|| unexpected("KeyError"))?;
    let kind = inner.get("resultType").ok_or_else(|| unexpected("KeyError"))?;
    let value = if kind.as_str() == Some("scalar") {
        index(result, 1)?
    } else {
        check(kind.as_str() == Some("vector") && result.is_array(), "expected a vector or scalar result")?;
        let series = result.as_array().unwrap();
        check(series.len() == 1, format!("query must return exactly one series, got {}", series.len()))?;
        let first = series[0].as_object().ok_or_else(|| unexpected("TypeError"))?;
        index(first.get("value").ok_or_else(|| unexpected("KeyError"))?, 1)?
    };
    let text = value.as_str().filter(|s| prom_number_ok(s)).ok_or("prometheus value is not a number")?;
    let number: f64 = match text {
        "NaN" => f64::NAN,
        "Inf" | "+Inf" => f64::INFINITY,
        "-Inf" => f64::NEG_INFINITY,
        t => t.parse().map_err(|_| unexpected("ValueError"))?,
    };
    check(number.is_finite(), "prometheus returned a non-finite value")?;
    Ok(number)
}

/// Python's `value[i]` on a JSON value: lists and strings index; dicts look up the int key.
fn index(value: &Value, i: usize) -> Res<Value> {
    match value {
        Value::Array(items) => items.get(i).cloned().ok_or_else(|| "unexpected prometheus response: IndexError".into()),
        Value::String(s) => s.chars().nth(i).map(|c| Value::String(c.to_string()))
            .ok_or_else(|| "unexpected prometheus response: IndexError".into()),
        Value::Object(_) => Err("unexpected prometheus response: KeyError".into()),
        _ => Err("unexpected prometheus response: TypeError".into()),
    }
}

// ---------------------------------------------------------------- OTLP traces

pub const SERVER_KIND: u128 = 2;
pub const ERROR_CODE: u128 = 2;
const CALLER_KINDS: [u128; 2] = [3, 4]; // CLIENT, PRODUCER
const CALLEE_KINDS: [u128; 2] = [2, 5]; // SERVER, CONSUMER
pub const MAX_PENDING_CALLS: usize = 100_000;
pub const MAX_CALL_PAIRS: usize = 1024;
pub const MAX_CALLS_BYTES: usize = 1024 * 1024;
pub const MAX_COUNT: u64 = (1 << 63) - 1;
pub const CALLS_KIND: &str = "ONTOLOGYEX_OBSERVED_CALLS";

/// (service.name, kind, status code, duration ms, trace ID, span ID, parent span ID);
/// kind and code are wide like Python ints, IDs are lowercase hex or empty.
pub type Span = (String, u128, u128, f64, String, String, String);

enum Field<'a> {
    Varint(u128),
    Fixed64(u64),
    Bytes(&'a [u8]),
    Fixed32,
}

fn varint(buf: &[u8], pos: &mut usize) -> Res<u128> {
    let mut result: u128 = 0;
    let mut shift = 0;
    loop {
        check(*pos < buf.len() && shift < 64, "truncated protobuf varint")?;
        let byte = buf[*pos];
        *pos += 1;
        result |= ((byte & 0x7F) as u128) << shift;
        if byte & 0x80 == 0 {
            return Ok(result);
        }
        shift += 7;
    }
}

/// `feeds.proto_fields`: every (field, value) of one message.
fn proto_fields(buf: &[u8]) -> Res<Vec<(u128, Field<'_>)>> {
    let mut fields = Vec::new();
    let mut pos = 0;
    while pos < buf.len() {
        let key = varint(buf, &mut pos)?;
        let (field, wire) = (key >> 3, key & 7);
        check(field > 0, "invalid protobuf field")?;
        let value = match wire {
            0 => Field::Varint(varint(buf, &mut pos)?),
            1 => {
                check(pos + 8 <= buf.len(), "truncated fixed64")?;
                let v = u64::from_le_bytes(buf[pos..pos + 8].try_into().unwrap());
                pos += 8;
                Field::Fixed64(v)
            }
            2 => {
                let size = varint(buf, &mut pos)?;
                check((pos as u128) + size <= buf.len() as u128, "truncated protobuf bytes")?;
                let size = size as usize;
                let v = &buf[pos..pos + size];
                pos += size;
                Field::Bytes(v)
            }
            5 => {
                check(pos + 4 <= buf.len(), "truncated fixed32")?;
                pos += 4;
                Field::Fixed32
            }
            _ => return Err("unsupported protobuf wire type".into()),
        };
        fields.push((field, value));
    }
    Ok(fields)
}

fn sub<'a>(fields: &[(u128, Field<'a>)], number: u128) -> Vec<&'a [u8]> {
    fields.iter().filter_map(|(f, v)| match v {
        Field::Bytes(b) if *f == number => Some(*b),
        _ => None,
    }).collect()
}

fn first_varint(fields: &[(u128, Field)], number: u128) -> u128 {
    fields.iter().find_map(|(f, v)| match v {
        Field::Varint(x) if *f == number => Some(*x),
        _ => None,
    }).unwrap_or(0)
}

fn first_fixed64(fields: &[(u128, Field)], number: u128) -> u64 {
    fields.iter().find_map(|(f, v)| match v {
        Field::Fixed64(x) if *f == number => Some(*x),
        _ => None,
    }).unwrap_or(0)
}

/// `feeds._proto_id`: the first bytes field `number` as hex if it is `size` bytes and not all zero.
fn proto_id(fields: &[(u128, Field)], number: u128, size: usize) -> String {
    match sub(fields, number).first() {
        Some(raw) if raw.len() == size && raw.iter().any(|b| *b != 0) => raw.iter().map(|b| format!("{b:02x}")).collect(),
        _ => String::new(),
    }
}

fn duration_ms(start: u64, end: u64) -> f64 {
    if end > start { (end - start) as f64 / 1e6 } else { 0.0 }
}

/// `feeds.spans_from_protobuf` (ExportTraceServiceRequest).
pub fn spans_from_protobuf(body: &[u8]) -> Res<Vec<Span>> {
    let mut out = Vec::new();
    for resource_spans in sub(&proto_fields(body)?, 1) {
        let rs = proto_fields(resource_spans)?;
        let mut service = String::new();
        for resource in sub(&rs, 1) {
            for kv in sub(&proto_fields(resource)?, 1) {
                let kvf = proto_fields(kv)?;
                let key: Vec<u8> = sub(&kvf, 1).concat();
                if String::from_utf8_lossy(&key) == "service.name" {
                    for any_value in sub(&kvf, 2) {
                        for text in sub(&proto_fields(any_value)?, 1) {
                            service = String::from_utf8_lossy(text).into_owned();
                        }
                    }
                }
            }
        }
        for scope_spans in sub(&rs, 2) {
            for span in sub(&proto_fields(scope_spans)?, 2) {
                let sf = proto_fields(span)?;
                let kind = first_varint(&sf, 6);
                let (start, end) = (first_fixed64(&sf, 7), first_fixed64(&sf, 8));
                let mut code = 0;
                for status in sub(&sf, 15) {
                    code = first_varint(&proto_fields(status)?, 3);
                }
                out.push((service.clone(), kind, code, duration_ms(start, end),
                          proto_id(&sf, 1, 16), proto_id(&sf, 2, 8), proto_id(&sf, 4, 8)));
            }
        }
    }
    Ok(out)
}

fn field<'a>(value: &'a Value, key: &str) -> Res<Option<&'a Value>> {
    let map = value.as_object().ok_or("invalid OTLP/JSON traces")?;
    Ok(map.get(key))
}

fn list_field<'a>(value: &'a Value, key: &str) -> Res<&'a [Value]> {
    match field(value, key)? {
        None => Ok(&[]),
        Some(Value::Array(items)) => Ok(items),
        _ => Err("invalid OTLP/JSON traces".into()),
    }
}

fn object_field<'a>(value: &'a Value, key: &str, empty: &'a Value) -> Res<&'a Value> {
    match field(value, key)? {
        None => Ok(empty),
        Some(v @ Value::Object(_)) => Ok(v),
        _ => Err("invalid OTLP/JSON traces".into()),
    }
}

fn string_field(value: &Value, key: &str) -> Res<String> {
    match field(value, key)? {
        None => Ok(String::new()),
        Some(Value::String(s)) => Ok(s.clone()),
        _ => Err("invalid OTLP/JSON traces".into()),
    }
}

fn enum_field(value: &Value, key: &str, names: &[(&str, u128)]) -> Res<u128> {
    match field(value, key)? {
        None => Ok(0),
        Some(Value::String(s)) => Ok(names.iter().find(|(n, _)| n == s).map_or(0, |(_, v)| *v)),
        Some(Value::Number(n)) if n.is_u64() => Ok(n.as_u64().unwrap() as u128),
        // A negative integer (down to -2**63) is never SERVER (2) or ERROR (2).
        Some(Value::Number(n)) if n.is_i64() => Ok(u128::MAX),
        _ => Err("invalid OTLP/JSON traces".into()),
    }
}

/// `feeds._json_id`: a hex ID string of `size` bytes, not all zero, lowercased; else empty.
fn json_id(span: &Value, key: &str, size: usize) -> Res<String> {
    let text = string_field(span, key)?;
    let valid = text.len() == 2 * size && text.bytes().all(|b| b.is_ascii_hexdigit()) && text.bytes().any(|b| b != b'0');
    Ok(if valid { text.to_ascii_lowercase() } else { String::new() })
}

fn nanos(value: Option<&Value>) -> Res<u64> {
    match value {
        None => Ok(0),
        Some(Value::String(s)) if (1..=20).contains(&s.len()) && s.bytes().all(|b| b.is_ascii_digit()) => {
            s.parse::<u64>().map_err(|_| "invalid OTLP/JSON traces".to_string())
        }
        Some(Value::Number(n)) if n.is_u64() => Ok(n.as_u64().unwrap()),
        _ => Err("invalid OTLP/JSON traces".into()),
    }
}

const KINDS: [(&str, u128); 6] = [("SPAN_KIND_SERVER", 2), ("SPAN_KIND_CLIENT", 3), ("SPAN_KIND_INTERNAL", 1),
                                  ("SPAN_KIND_PRODUCER", 4), ("SPAN_KIND_CONSUMER", 5), ("SPAN_KIND_UNSPECIFIED", 0)];
const CODES: [(&str, u128); 3] = [("STATUS_CODE_UNSET", 0), ("STATUS_CODE_OK", 1), ("STATUS_CODE_ERROR", 2)];

/// `feeds.spans_from_json` (OTLP/JSON).
pub fn spans_from_json(body: &[u8]) -> Res<Vec<Span>> {
    let data = json_document(body, "OTLP", false)?;
    let empty = json!({});
    let mut out = Vec::new();
    for rs in list_field(&data, "resourceSpans")? {
        let mut service = String::new();
        for kv in list_field(object_field(rs, "resource", &empty)?, "attributes")? {
            if string_field(kv, "key")? == "service.name" {
                service = string_field(object_field(kv, "value", &empty)?, "stringValue")?;
            }
        }
        for ss in list_field(rs, "scopeSpans")? {
            for span in list_field(ss, "spans")? {
                let kind = enum_field(span, "kind", &KINDS)?;
                let code = enum_field(object_field(span, "status", &empty)?, "code", &CODES)?;
                let start = nanos(field(span, "startTimeUnixNano")?)?;
                let end = nanos(field(span, "endTimeUnixNano")?)?;
                out.push((service.clone(), kind, code, duration_ms(start, end), json_id(span, "traceId", 16)?,
                          json_id(span, "spanId", 8)?, json_id(span, "parentSpanId", 8)?));
            }
        }
    }
    Ok(out)
}

// ---------------------------------------------------------------- observed calls file

/// Running totals per (caller, callee) service pair.
#[derive(Debug, Clone, PartialEq)]
pub struct CallTotal {
    pub calls: u64,
    pub errors: u64,
    pub first_seen: String,
    pub last_seen: String,
}

pub type CallTotals = BTreeMap<(String, String), CallTotal>;

fn stamp_ok(value: &Value) -> bool {
    value.as_str().is_some_and(|s| {
        let b = s.as_bytes();
        b.len() == 20 && b.iter().enumerate().all(|(i, c)| match i {
            4 | 7 => *c == b'-',
            10 => *c == b'T',
            13 | 16 => *c == b':',
            19 => *c == b'Z',
            _ => c.is_ascii_digit(),
        })
    })
}

fn count_ok(value: &Value) -> Option<u64> {
    value.as_u64().filter(|n| *n <= MAX_COUNT && !value.is_f64())
}

/// `feeds.read_calls`: the totals in a calls file, checked strictly.
pub fn read_calls(path: &Path) -> Res<CallTotals> {
    let document = json_document(&safe_read(path, MAX_CALLS_BYTES)?, "calls file", true)?;
    let top = document.as_object().filter(|m| {
        let keys: BTreeSet<&str> = m.keys().map(String::as_str).collect();
        keys == BTreeSet::from(["schema_version", "kind", "generated_at", "calls", "semantics"])
    }).ok_or("calls file: unexpected fields")?;
    check(as_int(&top["schema_version"]) == Some(1) && top["kind"] == CALLS_KIND, "unsupported calls file")?;
    check(stamp_ok(&top["generated_at"]), "calls file: invalid generated_at")?;
    let rows = top["calls"].as_array().filter(|r| r.len() <= MAX_CALL_PAIRS).ok_or("calls file: too many rows")?;
    let mut totals = CallTotals::new();
    for row in rows {
        let m = row.as_object().filter(|m| {
            let keys: BTreeSet<&str> = m.keys().map(String::as_str).collect();
            keys == BTreeSet::from(["caller_service", "callee_service", "caller", "callee", "calls", "errors",
                                    "first_seen", "last_seen"])
        }).ok_or("calls file: unexpected row fields")?;
        let (a, b) = (m["caller_service"].as_str().unwrap_or(""), m["callee_service"].as_str().unwrap_or(""));
        let key = (a.to_owned(), b.to_owned());
        check(service_ok(a) && service_ok(b) && a != b && !totals.contains_key(&key),
              "calls file: invalid or repeated service pair")?;
        for name in ["caller", "callee"] {
            check(m[name].is_null() || m[name].as_str().is_some_and(component_ok), "calls file: invalid component")?;
        }
        let (n, e) = (count_ok(&m["calls"]), count_ok(&m["errors"]));
        check(n.is_some_and(|n| n > 0) && e.is_some_and(|e| e <= n.unwrap()), "calls file: invalid counts")?;
        let (first, last) = (m["first_seen"].as_str().unwrap_or(""), m["last_seen"].as_str().unwrap_or(""));
        check(stamp_ok(&m["first_seen"]) && stamp_ok(&m["last_seen"]) && first <= last, "calls file: invalid times")?;
        totals.insert(key, CallTotal { calls: n.unwrap(), errors: e.unwrap(), first_seen: first.to_owned(),
                                       last_seen: last.to_owned() });
    }
    Ok(totals)
}

/// `feeds.merge_calls`: add what the window counted since the last merge to the totals.
pub fn merge_calls(totals: &mut CallTotals, seen: &mut BTreeMap<(String, String), (u64, u64)>,
                   current: &[(String, String, u64, u64)], moment: DateTime<Utc>) {
    let now = stamp(moment);
    for (caller, callee, calls, errors) in current {
        let key = (caller.clone(), callee.clone());
        let before = seen.insert(key.clone(), (*calls, *errors)).unwrap_or((0, 0));
        if *calls <= before.0 {
            continue;
        }
        if !totals.contains_key(&key) {
            if totals.len() >= MAX_CALL_PAIRS {
                continue;
            }
            totals.insert(key.clone(), CallTotal { calls: 0, errors: 0, first_seen: now.clone(), last_seen: now.clone() });
        }
        let row = totals.get_mut(&key).unwrap();
        row.calls = MAX_COUNT.min(row.calls.saturating_add(calls - before.0));
        row.errors = row.calls.min(row.errors.saturating_add(errors.saturating_sub(before.1)));
        row.last_seen = now.clone();
    }
}

/// `feeds.calls_document`.
pub fn calls_document(totals: &CallTotals, services: &BTreeMap<String, String>, moment: DateTime<Utc>) -> Value {
    let rows: Vec<Value> = totals.iter().map(|((a, b), t)| json!({
        "caller_service": a, "callee_service": b, "caller": services.get(a), "callee": services.get(b),
        "calls": t.calls, "errors": t.errors, "first_seen": t.first_seen, "last_seen": t.last_seen})).collect();
    json!({"schema_version": 1, "kind": CALLS_KIND, "generated_at": stamp(moment), "calls": rows,
           "semantics": "OBSERVED_IN_TRACES_NOT_CONFIRMED_DEPENDENCIES"})
}

/// `feeds.decode_request`: (200, or 201 for protobuf, spans), or (400 or 415, []).
pub fn decode_request(body: &[u8], content_type: &str, content_encoding: &str) -> (u16, Vec<Span>) {
    let mime = py_strip(content_type.split(';').next().unwrap_or(""));
    let encoding = py_strip(content_encoding).to_lowercase();
    let decoded;
    let body = if encoding == "gzip" {
        match gunzip(body) {
            Ok(b) => {
                decoded = b;
                &decoded[..]
            }
            Err(_) => return (400, Vec::new()),
        }
    } else if encoding.is_empty() || encoding == "identity" {
        body
    } else {
        return (415, Vec::new());
    };
    let result = match mime {
        "application/x-protobuf" => spans_from_protobuf(body).map(|s| (201, s)),
        "application/json" => spans_from_json(body).map(|s| (200, s)),
        _ => return (415, Vec::new()),
    };
    result.unwrap_or((400, Vec::new()))
}

/// `str.strip()` for ASCII header text (Python also strips \x1c-\x1f).
pub fn py_strip(s: &str) -> &str {
    s.trim_matches(|c: char| matches!(c, ' ' | '\t' | '\n' | '\r' | '\x0b' | '\x0c' | '\x1c'..='\x1f')
                    || (!c.is_ascii() && c.is_whitespace()))
}

#[cfg(feature = "feeds")]
fn gunzip(body: &[u8]) -> Res<Vec<u8>> {
    use flate2::read::GzDecoder;
    check(!body.is_empty(), "truncated gzip body")?;
    let mut out = Vec::new();
    GzDecoder::new(body).take(MAX_OTLP_DECOMPRESSED as u64 + 1).read_to_end(&mut out).map_err(|e| e.to_string())?;
    check(out.len() <= MAX_OTLP_DECOMPRESSED, "decompressed body too large")?;
    Ok(out)
}

#[cfg(not(feature = "feeds"))]
fn gunzip(_: &[u8]) -> Res<Vec<u8>> {
    Err("gzip support requires the feeds feature".into())
}

/// `feeds.p95_index`: index of the nearest-rank 95th percentile in n sorted values.
pub fn p95_index(n: usize) -> usize {
    ((0.95 * n as f64).ceil() as usize).saturating_sub(1)
}

/// Nearest-rank 95th percentile.
pub fn p95(values: &mut [f64]) -> f64 {
    values.sort_by(|a, b| a.total_cmp(b));
    values[p95_index(values.len())]
}

/// The bounded tables `tests/feeds_conformance.py` compares with Python and Lean:
/// classification over two metrics with thresholds 0..3, retention of every key
/// list up to length 5 over 0..3, and the p95 index for 1..100000 samples.
pub fn conformance_table() -> Value {
    let names = ["error_rate_pct", "p95_latency_ms"];
    let assoc = |i: usize| -> BTreeMap<String, f64> {
        let mut m = BTreeMap::new();
        for (slot, value) in [(0, i / 5), (1, i % 5)] {
            if value > 0 {
                m.insert(names[slot].to_string(), (value - 1) as f64);
            }
        }
        m
    };
    let mut classify_rows = Vec::new();
    for mi in 0..25 {
        for wi in 0..25 {
            for ei in 0..25 {
                for down in [false, true] {
                    classify_rows.push(json!(classify(&assoc(mi), &Thresholds(assoc(wi)), &Thresholds(assoc(ei)), down)));
                }
            }
        }
    }
    let mut retain_rows = Vec::new();
    let mut lists: Vec<Vec<usize>> = vec![vec![]];
    let mut frontier = lists.clone();
    for _ in 0..5 {
        frontier = frontier.iter().flat_map(|p| (0..4).map(move |v| [p.clone(), vec![v]].concat())).collect();
        lists.extend(frontier.iter().cloned());
    }
    for keys in &lists {
        for keep in [2, 3, 4] {
            let rows: Vec<Row> = keys.iter().map(|v| Row {
                component_id: "discovery:service:compose.yml:db".into(), observed_at: format!("2026-10-28T08:00:0{v}Z"),
                status: "ok".into(), source: "otel".into(), expires_after_seconds: 60, metrics: BTreeMap::new(),
            }).collect();
            let kept: Vec<usize> = retain(rows, keep).iter().map(|r| (r.observed_at.as_bytes()[18] - b'0') as usize).collect();
            retain_rows.push(json!(kept));
        }
    }
    let p95_rows: Vec<usize> = (1..=MAX_SPANS_PER_SERVICE).map(p95_index).collect();
    json!({"kind": "FEEDS_CONFORMANCE_TABLE", "classify": classify_rows, "retain": retain_rows,
           "p95": p95_rows, "execution_authorized": false})
}

/// Unmatched halves of calls in arrival order, like Python's OrderedDict: a key map
/// plus an insertion sequence, so the oldest is found and any key removed cheaply.
pub struct Pending<V> {
    entries: HashMap<(String, String), (u64, f64, V)>,
    order: BTreeMap<u64, (String, String)>,
    next: u64,
}

impl<V> Pending<V> {
    fn new() -> Self {
        Pending { entries: HashMap::new(), order: BTreeMap::new(), next: 0 }
    }
    pub fn len(&self) -> usize {
        self.entries.len()
    }
    fn expire(&mut self, cutoff: f64) {
        while let Some((&seq, key)) = self.order.first_key_value() {
            if self.entries[key].1 >= cutoff {
                break;
            }
            let key = key.clone();
            self.order.remove(&seq);
            self.entries.remove(&key);
        }
    }
    fn take(&mut self, key: &(String, String)) -> Option<V> {
        let (seq, _, value) = self.entries.remove(key)?;
        self.order.remove(&seq);
        Some(value)
    }
    fn insert(&mut self, key: (String, String), arrived: f64, value: V) {
        self.order.insert(self.next, key.clone());
        self.entries.insert(key, (self.next, arrived, value));
        self.next += 1;
    }
}

/// Server spans per service over a sliding window, timed by arrival (not span clocks),
/// and calls between services from client and server spans that pair up.
pub struct SpanWindow {
    pub services: BTreeMap<String, String>,
    pub window: f64,
    pub emit: i64,
    pub min_requests: usize,
    pub warning: Thresholds,
    pub error: Thresholds,
    spans: BTreeMap<String, VecDeque<(f64, bool, f64)>>,
    pub unmapped: BTreeMap<String, u64>,
    pub accepted: u64,
    pub callers: Pending<String>,
    pub callees: Pending<(String, bool)>,
    calls: BTreeMap<(String, String), (u64, u64)>,
    pub calls_dropped: u64,
}

impl SpanWindow {
    pub fn new(otlp: &Otlp) -> Self {
        SpanWindow { services: otlp.services.clone(), window: otlp.window as f64, emit: otlp.emit,
                     min_requests: otlp.min_requests, warning: otlp.warning.clone(), error: otlp.error.clone(),
                     spans: BTreeMap::new(), unmapped: BTreeMap::new(), accepted: 0,
                     callers: Pending::new(), callees: Pending::new(), calls: BTreeMap::new(), calls_dropped: 0 }
    }

    pub fn ingest(&mut self, spans: &[Span], arrived: f64) -> usize {
        let mut count = 0;
        self.callers.expire(arrived - self.window);
        self.callees.expire(arrived - self.window);
        for (service, kind, code, duration, trace, span_id, parent) in spans {
            let failed = *code == ERROR_CODE;
            if !trace.is_empty() && !span_id.is_empty() && CALLER_KINDS.contains(kind) {
                let key = (trace.clone(), span_id.clone());
                if let Some((callee, callee_failed)) = self.callees.take(&key) {
                    self.pair(service, &callee, callee_failed);
                } else if !self.callers.entries.contains_key(&key) {
                    if self.callers.len() >= MAX_PENDING_CALLS {
                        self.calls_dropped += 1;
                    } else {
                        self.callers.insert(key, arrived, service.clone());
                    }
                }
            } else if !trace.is_empty() && !parent.is_empty() && CALLEE_KINDS.contains(kind) {
                let key = (trace.clone(), parent.clone());
                if let Some(caller) = self.callers.take(&key) {
                    self.pair(&caller, service, failed);
                } else if !self.callees.entries.contains_key(&key) {
                    if self.callees.len() >= MAX_PENDING_CALLS {
                        self.calls_dropped += 1;
                    } else {
                        self.callees.insert(key, arrived, (service.clone(), failed));
                    }
                }
            }
            if *kind != SERVER_KIND {
                continue;
            }
            if !self.services.contains_key(service) {
                if self.unmapped.len() < 64 || self.unmapped.contains_key(service) {
                    *self.unmapped.entry(service.clone()).or_insert(0) += 1;
                }
                continue;
            }
            let queue = self.spans.entry(service.clone()).or_default();
            if queue.len() == MAX_SPANS_PER_SERVICE {
                queue.pop_front();
            }
            queue.push_back((arrived, failed, *duration));
            count += 1;
        }
        self.accepted += count as u64;
        count
    }

    /// One observed call. Calls within one service, or from an unnamed one, are not edges.
    fn pair(&mut self, caller: &str, callee: &str, failed: bool) {
        if caller == callee || !service_ok(caller) || !service_ok(callee) {
            return;
        }
        let key = (caller.to_owned(), callee.to_owned());
        if !self.calls.contains_key(&key) && self.calls.len() >= MAX_CALL_PAIRS {
            self.calls_dropped += 1;
            return;
        }
        let row = self.calls.entry(key).or_insert((0, 0));
        row.0 += 1;
        row.1 += failed as u64;
    }

    /// (caller, callee, calls, errors) since start, sorted.
    pub fn call_counts(&self) -> Vec<(String, String, u64, u64)> {
        self.calls.iter().map(|((a, b), (n, e))| (a.clone(), b.clone(), *n, *e)).collect()
    }

    pub fn observations(&mut self, arrived: f64, moment: DateTime<Utc>) -> Vec<Row> {
        let mut rows = Vec::new();
        for (service, queue) in self.spans.iter_mut() {
            while queue.front().is_some_and(|(t, _, _)| *t < arrived - self.window) {
                queue.pop_front();
            }
            if queue.len() < self.min_requests {
                continue; // too little traffic to say anything; the last sample expires
            }
            let errors = queue.iter().filter(|(_, failed, _)| *failed).count();
            let mut durations: Vec<f64> = queue.iter().map(|(_, _, d)| *d).collect();
            let mut metrics = BTreeMap::new();
            metrics.insert("error_rate_pct".to_string(), 100.0 * errors as f64 / queue.len() as f64);
            metrics.insert("p95_latency_ms".to_string(), p95(&mut durations));
            let status = classify(&metrics, &self.warning, &self.error, false);
            rows.push(observation(&self.services[service], status, "otel", self.emit, &metrics, moment));
        }
        rows
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn component_ids() {
        assert!(component_ok("discovery:service:compose.yml:api"));
        assert!(component_ok("discovery:a_b:x"));
        assert!(!component_ok("discovery::x"));
        assert!(!component_ok("discovery:service:"));
        assert!(!component_ok("discovery:Service:x"));
    }

    #[test]
    fn urls_follow_python_rules() {
        let hosts: BTreeSet<String> = ["api.internal", "127.0.0.1", "::1", "0x7f.1"].iter().map(|s| s.to_string()).collect();
        for ok in ["http://api.internal/healthz", "HTTPS://API.internal:8443/x?y=1", "http://[::1]:9090", "http://127.0.0.1"] {
            assert!(url(Some(&json!(ok)), &hosts, "t").is_ok(), "{ok}");
        }
        for bad in ["http://api.internal/#x", "ftp://api.internal/", "http://u@api.internal/", "http://api.internal:99999/",
                    "http://api.internal:8a/", "http://0x7f.1/", "http://[127.0.0.1]/", "http://api.internal/a[b]",
                    "http://api .internal/", "http:api.internal", "http://other/"] {
            assert!(url(Some(&json!(bad)), &hosts, "t").is_err(), "{bad}");
        }
    }

    #[test]
    fn timestamps_normalize_like_python() {
        assert_eq!(normalize_timestamp("2026-10-28T10:00:00+02:00").unwrap(), "2026-10-28T08:00:00Z");
        assert_eq!(normalize_timestamp("2026-10-28T08:00:00.5Z").unwrap(), "2026-10-28T08:00:00.500000Z");
        assert_eq!(normalize_timestamp("2026-10-28T08:00:00.1234567Z").unwrap(), "2026-10-28T08:00:00.123456Z");
        assert_eq!(normalize_timestamp("2026-10-28T08:00Z").unwrap(), "2026-10-28T08:00:00Z");
        assert!(normalize_timestamp("2026-10-28T24:00:00Z").is_err());
        assert!(normalize_timestamp("2026-10-28 08:00:00Z").is_err());
        assert!(normalize_timestamp("0001-01-01T00:00:00+01:00").is_err());
    }

    #[test]
    fn prometheus_parsing() {
        let body = br#"{"status":"success","data":{"resultType":"vector","result":[{"metric":{},"value":[1,"2.5"]}]}}"#;
        assert_eq!(prometheus_number(body).unwrap(), 2.5);
        let scalar = br#"{"status":"success","data":{"resultType":"scalar","result":[1,"1e3"]}}"#;
        assert_eq!(prometheus_number(scalar).unwrap(), 1000.0);
        for bad in [&br#"[]"#[..], br#"{"status":"success","data":{"resultType":"vector","result":[]}}"#,
                    br#"{"status":"success","data":{"resultType":"scalar","result":[1,"NaN"]}}"#,
                    br#"{"status":"success","data":{"resultType":"scalar","result":[1," 1"]}}"#] {
            assert!(prometheus_number(bad).is_err());
        }
        assert_eq!(prometheus_query_url("http://p:9090/", "up{job=\"a b\"}"), "http://p:9090/api/v1/query?query=up%7Bjob%3D%22a+b%22%7D");
    }

    #[test]
    fn window_matches_python_math() {
        let otlp = Otlp { services: [("api".to_string(), "discovery:service:c.yml:api".to_string())].into(), bind: String::new(),
                          port: 0, token_env: String::new(), window: 60, emit: 15, min_requests: 2,
                          warning: Thresholds([("p95_latency_ms".to_string(), 100.0)].into()), error: Thresholds::default() };
        let mut w = SpanWindow::new(&otlp);
        let spans: Vec<Span> = (0..20).map(|i| ("api".to_string(), 2, if i == 0 { 2 } else { 0 }, i as f64 * 10.0,
                                                 String::new(), String::new(), String::new())).collect();
        assert_eq!(w.ingest(&spans, 0.0), 20);
        let rows = w.observations(1.0, DateTime::from_timestamp(0, 0).unwrap());
        assert_eq!(rows[0].status, "warning");
        assert_eq!(rows[0].line(), r#"{"component_id":"discovery:service:c.yml:api","expires_after_seconds":60,"metrics":{"error_rate_pct":5.0,"p95_latency_ms":180.0},"observed_at":"1970-01-01T00:00:00Z","schema_version":1,"source":"otel","status":"warning"}"#);
    }
}
