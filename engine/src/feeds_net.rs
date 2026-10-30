//! Network side of the live feeds: HTTP probes, Prometheus instant queries,
//! the OTLP/HTTP receiver, and the polling loop. Mirrors `feeds.FeedRunner`,
//! except that probes and Prometheus targets are polled in parallel.
//!
//! Outbound requests go only to allowlisted hosts, never through a proxy, and
//! never follow redirects (a redirect is an answer, not a new target).

use crate::feeds::{self, classify, decode_request, observation, stamp, CallTotals, Config, Probe, Prometheus,
                   PromTarget, Res, Row, SpanWindow, Thresholds, MAX_BODY, MAX_OTLP_BODY};
use crate::pyjson;
use chrono::Utc;
use serde_json::{json, Map, Value};
use std::collections::BTreeMap;
use std::io::Read;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

const PARALLEL_POLLS: usize = 16;
const OTLP_WORKERS: usize = 4;

/// (status, body, elapsed ms, error name)
pub type Fetched = (Option<u16>, Vec<u8>, f64, String);

fn error_name(error: &ureq::Error) -> String {
    match error {
        ureq::Error::Timeout(_) => "TimeoutError".into(),
        ureq::Error::HostNotFound => "gaierror".into(),
        // rustls reports certificate and handshake failures as I/O errors.
        ureq::Error::Io(e) if e.to_string().to_lowercase().contains("certificate") => "SSLCertVerificationError".into(),
        ureq::Error::Io(e) if ["tls", "handshake", "alert"].iter().any(|w| e.to_string().to_lowercase().contains(w)) => {
            "SSLError".into()
        }
        ureq::Error::Io(e) => match e.kind() {
            std::io::ErrorKind::ConnectionRefused => "ConnectionRefusedError".into(),
            std::io::ErrorKind::ConnectionReset => "ConnectionResetError".into(),
            std::io::ErrorKind::TimedOut => "TimeoutError".into(),
            _ => "OSError".into(),
        },
        ureq::Error::Tls(_) | ureq::Error::Rustls(_) => "SSLError".into(),
        _ => "URLError".into(),
    }
}

/// GET with a byte cap, no redirects, no proxy, and the system trust store.
pub fn fetch(url: &str, timeout: f64, headers: &[(&str, String)]) -> Fetched {
    let limit = Some(Duration::from_secs_f64(timeout));
    let tls = ureq::tls::TlsConfig::builder().root_certs(ureq::tls::RootCerts::PlatformVerifier).build();
    let agent: ureq::Agent = ureq::Agent::config_builder()
        .max_redirects(0)
        .max_redirects_will_error(false)
        .http_status_as_error(false)
        .proxy(None)
        .user_agent("ontologyex-feeds/1")
        .tls_config(tls)
        .timeout_resolve(limit)
        .timeout_connect(limit)
        .timeout_send_request(limit)
        .timeout_recv_response(limit)
        .timeout_recv_body(limit)
        .build()
        .into();
    let started = Instant::now();
    let mut request = agent.get(url);
    for (name, value) in headers {
        request = request.header(*name, value.as_str());
    }
    let elapsed = |s: Instant| s.elapsed().as_secs_f64() * 1000.0;
    match request.call() {
        Ok(mut response) => {
            let status = response.status().as_u16();
            let mut body = Vec::new();
            let read = response.body_mut().with_config().limit(u64::MAX).reader()
                .take(MAX_BODY as u64 + 1).read_to_end(&mut body);
            let ms = elapsed(started);
            match read {
                Err(_) => (Some(status), Vec::new(), ms, "IncompleteRead".into()),
                Ok(_) if body.len() > MAX_BODY => (Some(status), Vec::new(), ms, "RESPONSE_TOO_LARGE".into()),
                Ok(_) => (Some(status), body, ms, String::new()),
            }
        }
        Err(e) => (None, Vec::new(), elapsed(started), error_name(&e)),
    }
}

pub type Getter = dyn Fn(&str, f64, &[(&str, String)]) -> Fetched + Sync;

/// `feeds.run_probe`: the row, and a detail for the status file.
pub fn run_probe(probe: &Probe, getter: &Getter) -> (Row, String) {
    let (status, _, elapsed, error) = getter(&probe.url, probe.timeout as f64, &[]);
    let down = status.is_none_or(|s| !probe.expect.contains(&(s as i64)));
    let mut metrics = BTreeMap::new();
    if status.is_some() {
        metrics.insert("probe_latency_ms".to_string(), elapsed);
    }
    let state = classify(&metrics, &probe.warning, &Thresholds::default(), down);
    let detail = if !error.is_empty() {
        error
    } else if down {
        format!("HTTP {}", status.unwrap_or(0))
    } else {
        String::new()
    };
    (observation(&probe.component, state, "health_probe", probe.interval, &metrics, Utc::now()), detail)
}

fn prometheus_value(prom: &Prometheus, query: &str, token: &str, getter: &Getter) -> Res<f64> {
    let url = feeds::prometheus_query_url(&prom.url, query);
    let headers: Vec<(&str, String)> = if token.is_empty() { vec![] } else { vec![("Authorization", format!("Bearer {token}"))] };
    let (status, body, _, error) = getter(&url, prom.timeout as f64, &headers);
    if status != Some(200) {
        let why = if error.is_empty() { status.map_or("None".into(), |s| s.to_string()) } else { error };
        return Err(format!("prometheus answered {why}"));
    }
    feeds::prometheus_number(&body)
}

/// `feeds.run_prometheus`.
pub fn run_prometheus(prom: &Prometheus, target: &PromTarget, getter: &Getter) -> Res<Row> {
    let token = if prom.token_env.is_empty() { String::new() } else { std::env::var(&prom.token_env).unwrap_or_default() };
    let mut metrics = BTreeMap::new();
    let mut down = false;
    if let Some(up) = &target.up {
        down = prometheus_value(prom, up, &token, getter)? < 1.0;
    }
    for (name, query) in [("error_rate_pct", &target.error_rate_pct), ("p95_latency_ms", &target.p95_latency_ms),
                          ("job_lag_seconds", &target.job_lag_seconds)] {
        if let Some(q) = query {
            metrics.insert(name.to_string(), prometheus_value(prom, q, &token, getter)?);
        }
    }
    let state = classify(&metrics, &target.warning, &target.error, down);
    Ok(observation(&target.component, state, "prometheus", prom.interval, &metrics, Utc::now()))
}

/// Feed status in first-seen order (Python dict order), for `once` output.
#[derive(Default)]
pub struct Status(Vec<(String, Value)>);

impl Status {
    fn record(&mut self, feed: String, value: Value) {
        match self.0.iter_mut().find(|(k, _)| *k == feed) {
            Some(slot) => slot.1 = value,
            None => self.0.push((feed, value)),
        }
    }
    fn entry(ok: bool, detail: &str) -> Value {
        json!({"last_run": stamp(Utc::now()), "ok": ok, "detail": detail.chars().take(200).collect::<String>()})
    }
    pub fn as_object(&self) -> Map<String, Value> {
        self.0.iter().cloned().collect()
    }
    /// `json.dumps(status, indent=2)` without sorting, nested at `indent` levels.
    pub fn ordered_json(&self, level: usize) -> String {
        if self.0.is_empty() {
            return "{}".into();
        }
        let pad = "  ".repeat(level + 1);
        let items: Vec<String> = self.0.iter().map(|(k, v)| {
            let inner = pyjson::dumps(v, Some(2), true).replace('\n', &format!("\n{pad}"));
            format!("{pad}{}: {}", pyjson::dumps(&Value::String(k.clone()), None, true), inner)
        }).collect();
        format!("{{\n{}\n{}}}", items.join(",\n"), "  ".repeat(level))
    }
}

pub struct Runner {
    pub config: Config,
    pub status: Status,
    pub window: Option<Arc<Mutex<SpanWindow>>>,
    pub listening: Option<String>,
    started: Instant,
    getter: Box<Getter>,
    call_totals: CallTotals,
    calls_seen: BTreeMap<(String, String), (u64, u64)>,
}

impl Runner {
    pub fn new(config: Config) -> Res<Self> {
        let window = config.otlp.as_ref().map(|o| Arc::new(Mutex::new(SpanWindow::new(o))));
        // Calls counted before a restart stay; an unreadable file is refused, not overwritten.
        let calls_path = config.calls_path();
        let call_totals = if window.is_some() && calls_path.exists() { feeds::read_calls(&calls_path)? } else { CallTotals::new() };
        Ok(Runner { config, status: Status::default(), window, listening: None, started: Instant::now(), getter: Box::new(fetch),
                    call_totals, calls_seen: BTreeMap::new() })
    }

    fn monotonic(&self) -> f64 {
        self.started.elapsed().as_secs_f64()
    }

    /// One round of probes and Prometheus queries; returns rows written.
    pub fn poll_once(&mut self) -> Res<usize> {
        enum Job<'a> {
            Probe(&'a Probe),
            Prom(&'a Prometheus, &'a PromTarget),
        }
        let mut jobs: Vec<Job> = self.config.probes.iter().map(Job::Probe).collect();
        if let Some(prom) = &self.config.prometheus {
            jobs.extend(prom.targets.iter().map(|t| Job::Prom(prom, t)));
        }
        let getter = &*self.getter;
        // Poll in parallel, then record in configuration order like the Python runner.
        let mut results: Vec<(String, Res<Row>, String)> = Vec::new();
        for chunk in jobs.chunks(PARALLEL_POLLS) {
            let chunk_results: Vec<(String, Res<Row>, String)> = thread::scope(|scope| {
                let handles: Vec<_> = chunk.iter().map(|job| scope.spawn(move || match job {
                    Job::Probe(p) => {
                        let (row, detail) = run_probe(p, getter);
                        (format!("probe {}", p.component), Ok(row), detail)
                    }
                    Job::Prom(prom, t) => (format!("prometheus {}", t.component), run_prometheus(prom, t, getter), String::new()),
                })).collect();
                handles.into_iter().map(|h| h.join().expect("poll thread")).collect()
            });
            results.extend(chunk_results);
        }
        let mut rows = Vec::new();
        for (name, result, detail) in results {
            match result {
                Ok(row) if name.starts_with("probe ") => {
                    // The feed worked: a 503 or a refused connection is a verdict about the target.
                    let text = format!("target {}{}", row.status, if detail.is_empty() { String::new() } else { format!(" ({detail})") });
                    self.status.record(name, Status::entry(true, &text));
                    rows.push(row);
                }
                Ok(row) => {
                    self.status.record(name, Status::entry(true, ""));
                    rows.push(row);
                }
                Err(e) => self.status.record(name, Status::entry(false, &e)), // the last sample expires to STALE
            }
        }
        let written = feeds::store_add(&self.config.output, self.config.keep, rows)?;
        self.write_status()?;
        Ok(written)
    }

    pub fn emit_otlp(&mut self) -> Res<usize> {
        let Some(window) = self.window.clone() else { return Ok(0) };
        let (rows, accepted, unmapped, calls) = {
            let mut w = window.lock().map_err(|_| "span window poisoned")?;
            (w.observations(self.monotonic(), Utc::now()), w.accepted, w.unmapped.clone(), w.call_counts())
        };
        let written = feeds::store_add(&self.config.output, self.config.keep, rows)?;
        self.status.record("otlp".into(), json!({"last_run": stamp(Utc::now()), "ok": true,
                                                 "detail": format!("{accepted} server spans accepted"),
                                                 "unmapped_services": unmapped}));
        self.write_calls(&calls)?;
        self.write_status()?;
        Ok(written)
    }

    fn write_calls(&mut self, current: &[(String, String, u64, u64)]) -> Res<()> {
        let moment = Utc::now();
        feeds::merge_calls(&mut self.call_totals, &mut self.calls_seen, current, moment);
        let services = &self.config.otlp.as_ref().ok_or("no otlp configuration")?.services;
        let document = feeds::calls_document(&self.call_totals, services, moment);
        feeds::atomic_write(&self.config.calls_path(), pyjson::dumps(&document, Some(2), true).as_bytes(), ".feed-calls-", false)
    }

    pub fn write_status(&self) -> Res<()> {
        let document = json!({"schema_version": 1, "kind": "ONTOLOGYEX_FEED_STATUS", "generated_at": stamp(Utc::now()),
                              "feeds": self.status.as_object(), "otlp_listening": self.listening,
                              "semantics": "SOURCE_REPORTED_TELEMETRY_NOT_INDEPENDENTLY_ATTESTED"});
        feeds::atomic_write(&self.config.status_path(), pyjson::dumps(&document, Some(2), true).as_bytes(), ".feed-status-", false)
    }

    /// Start the OTLP/HTTP receiver on worker threads; returns the bound address.
    pub fn start_otlp(&mut self) -> Res<String> {
        let otlp = self.config.otlp.clone().ok_or("no otlp configuration")?;
        let token = if otlp.token_env.is_empty() { String::new() } else { std::env::var(&otlp.token_env).unwrap_or_default() };
        if !otlp.token_env.is_empty() && token.is_empty() {
            return Err(format!("environment variable {} is empty", otlp.token_env));
        }
        let host = if otlp.bind.contains(':') { format!("[{}]", otlp.bind) } else { otlp.bind.clone() };
        let server = tiny_http::Server::http(format!("{host}:{}", otlp.port)).map_err(|e| e.to_string())?;
        let address = server.server_addr().to_ip().map(|a| a.to_string()).unwrap_or_default();
        let server = Arc::new(server);
        let window = self.window.clone().ok_or("no span window")?;
        let started = self.started;
        for _ in 0..OTLP_WORKERS {
            let (server, window, token) = (server.clone(), window.clone(), token.clone());
            thread::spawn(move || {
                while let Ok(request) = server.recv() {
                    handle(request, &window, &token, started);
                }
            });
        }
        self.listening = Some(address.clone());
        Ok(address)
    }

    /// Poll and emit until `stop` is set or a write fails.
    pub fn run(&mut self, stop: &AtomicBool) -> Res<()> {
        let mut intervals: Vec<i64> = self.config.probes.iter().map(|p| p.interval).collect();
        if let Some(prom) = &self.config.prometheus {
            intervals.push(prom.interval);
        }
        let poll_every = intervals.iter().min().map(|s| *s as f64);
        let emit_every = self.config.otlp.as_ref().map(|o| o.emit as f64);
        let (mut next_poll, mut next_emit) = (self.monotonic(), self.monotonic());
        while !stop.load(Ordering::Relaxed) {
            let now = self.monotonic();
            if let Some(every) = poll_every.filter(|_| now >= next_poll) {
                self.poll_once()?;
                next_poll = now + every;
            }
            if let Some(every) = emit_every.filter(|_| now >= next_emit) {
                self.emit_otlp()?;
                next_emit = now + every;
            }
            thread::sleep(Duration::from_millis(500));
        }
        Ok(())
    }
}

fn header<'a>(request: &'a tiny_http::Request, name: &'static str) -> &'a str {
    request.headers().iter().find(|h| h.field.equiv(name)).map_or("", |h| h.value.as_str())
}

fn reply(request: tiny_http::Request, code: u16, body: &[u8], mime: &str) {
    let content_type = tiny_http::Header::from_bytes("Content-Type", mime).expect("ascii header");
    let _ = request.respond(tiny_http::Response::from_data(body.to_vec()).with_status_code(code).with_header(content_type));
}

/// Constant-time comparison of the Authorization header with the expected value.
fn same(a: &[u8], b: &[u8]) -> bool {
    a.len() == b.len() && a.iter().zip(b).fold(0u8, |acc, (x, y)| acc | (x ^ y)) == 0
}

/// `feeds.otlp_handler.do_POST`.
fn handle(mut request: tiny_http::Request, window: &Mutex<SpanWindow>, token: &str, started: Instant) {
    const JSON: &str = "application/json";
    if *request.method() != tiny_http::Method::Post {
        return reply(request, 501, b"{\"error\":\"unsupported method\"}", JSON);
    }
    if crate::feeds::request_path(request.url()) != "/v1/traces" {
        return reply(request, 404, br#"{"error":"only /v1/traces is accepted"}"#, JSON);
    }
    if !token.is_empty() && !same(header(&request, "Authorization").as_bytes(), format!("Bearer {token}").as_bytes()) {
        return reply(request, 401, br#"{"error":"bearer token required"}"#, JSON);
    }
    let mut body = Vec::new();
    match request.body_length() {
        Some(n) if n <= MAX_OTLP_BODY => {
            if request.as_reader().take(n as u64).read_to_end(&mut body).is_err() || body.len() != n {
                return reply(request, 400, br#"{"error":"invalid OTLP traces"}"#, JSON);
            }
        }
        // `feeds.read_chunked`: OpenTelemetry JS sends chunked bodies without a length.
        None if feeds::py_strip(header(&request, "Transfer-Encoding")).eq_ignore_ascii_case("chunked") => {
            if request.as_reader().take(MAX_OTLP_BODY as u64 + 1).read_to_end(&mut body).is_err() {
                return reply(request, 400, br#"{"error":"invalid chunked body"}"#, JSON);
            }
            if body.len() > MAX_OTLP_BODY {
                return reply(request, 413, br#"{"error":"body too large"}"#, JSON);
            }
        }
        _ => return reply(request, 413, br#"{"error":"body too large or unknown length"}"#, JSON),
    }
    let (code, spans) = decode_request(&body, header(&request, "Content-Type"), header(&request, "Content-Encoding"));
    match code {
        415 => reply(request, 415, br#"{"error":"use gzip or no compression, and application/x-protobuf or application/json"}"#, JSON),
        400 => reply(request, 400, br#"{"error":"invalid OTLP traces"}"#, JSON),
        _ => {
            if let Ok(mut w) = window.lock() {
                w.ingest(&spans, started.elapsed().as_secs_f64());
            }
            if code == 201 {
                reply(request, 200, b"", "application/x-protobuf"); // empty ExportTraceServiceResponse
            } else {
                reply(request, 200, b"{}", JSON);
            }
        }
    }
}
